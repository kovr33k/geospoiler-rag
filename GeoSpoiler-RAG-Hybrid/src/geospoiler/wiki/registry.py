"""Approved Wiki concept registry, surface discovery, proposals, and review.

The registry is deliberately conservative:

* structured entity/topic surfaces are discovered from current eligible cards;
* ordinary candidates require two distinct substantive content clusters from
  two independent source families;
* one substantive YouTube segment may propose its own primary topic, while the
  parent video and sibling segments never multiply incidental mentions;
* media-source entities remain provenance/search metadata and never become Wiki
  concepts or relation surfaces;
* a proposal never creates a concept until an explicit review decision approves it;
* identity aliases are explicit approved facts, never contextual metonyms;
* rejected and reopened decisions remain in immutable history.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from geospoiler.wiki.hashing import canonical_json, content_hash, normalize_text, sha256_hex
from geospoiler.wiki.state import get_dependency_head, publish_dependency

ConceptKind = Literal["entity", "topic"]
ProposalKind = Literal["entity", "topic", "alias", "split", "merge"]
ProposalStatus = Literal["pending", "approved", "rejected", "deferred"]
ReviewDecisionKind = Literal["approved", "rejected", "deferred", "reopened"]
AliasKind = Literal["canonical", "technical", "abbreviation", "translation", "spelling"]

MIN_PROPOSAL_CLUSTERS = 2
EXCLUDED_ENTITY_CATEGORIES = frozenset({"media_sources"})
ENTITY_CATEGORIES = (
    "people",
    "organizations",
    "countries",
    "locations",
    "military_units",
    "equipment",
    "weapons",
    "programs_projects",
    "media_sources",
    "other",
)


class RegistryError(RuntimeError):
    """Base error for registry and review operations."""


class ProposalNotFoundError(RegistryError):
    """Raised when a requested proposal does not exist."""


class ProposalStateError(RegistryError):
    """Raised when a review transition is not allowed."""


class ConceptNotFoundError(RegistryError):
    """Raised when an approved concept does not exist."""


@dataclass(frozen=True)
class SurfaceEvidence:
    normalized_surface: str
    display_surface: str
    proposal_kind: ConceptKind
    candidate_key: str
    source_category: str
    salience: str
    source_role: str
    source_kind: str
    source_family_id: str
    source_lineage_id: str
    card_revision_id: str
    content_cluster_id: str
    source_locator: dict[str, Any]


@dataclass(frozen=True)
class RegistryScanStats:
    cards_seen: int
    cards_eligible: int
    surfaces_seen: int
    surfaces_unresolved: int
    proposals_created: int
    evidence_created: int
    surface_revisions_changed: int


@dataclass(frozen=True)
class ConceptProposal:
    proposal_id: str
    proposal_kind: ProposalKind
    normalized_candidate_key: str
    display_label: str
    source_category: str
    status: ProposalStatus
    evidence_count: int
    cluster_count: int
    candidate_concept_ids: tuple[str, ...]
    payload: dict[str, Any]
    created_at: str
    latest_rationale: str | None


@dataclass(frozen=True)
class ApprovedConcept:
    concept_id: str
    concept_kind: ConceptKind
    canonical_key: str
    canonical_label: str
    description: str
    source_category: str
    concept_revision_id: str
    generation: int
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResult:
    proposal_id: str
    decision: ReviewDecisionKind
    decision_generation: int
    concept_id: str | None


@dataclass(frozen=True)
class RegistryDependencyStats:
    concept_snapshots_changed: int
    display_snapshots_changed: int
    alias_snapshots_changed: int
    surface_snapshots_changed: int


def create_identity_group_proposal(
    connection: sqlite3.Connection,
    *,
    member_proposal_ids: Sequence[str],
    canonical_member_proposal_id: str,
    canonical_label: str,
    alias_kinds: Mapping[str, AliasKind],
    rationale: str,
    resolver_metadata: Mapping[str, Any] | None = None,
) -> ConceptProposal:
    """Propose a canonicalization or identity group for pending surfaces.

    This is the reviewable form of a cross-language/abbreviation decision. It
    does not approve any member or create a concept by itself.
    """
    members = tuple(sorted(set(member_proposal_ids)))
    if not members:
        raise ValueError("An identity review requires at least one member proposal")
    if canonical_member_proposal_id not in members:
        raise ValueError("canonical_member_proposal_id must be a group member")
    proposals = [get_proposal(connection, proposal_id) for proposal_id in members]
    if any(proposal.status != "pending" for proposal in proposals):
        raise ProposalStateError("Every identity-group member must still be pending")
    member_kinds = {
        "topic" if proposal.proposal_kind == "topic" else "entity"
        for proposal in proposals
    }
    if len(member_kinds) != 1:
        raise ValueError("Identity groups cannot mix entity and topic proposals")
    if member_kinds == {"entity"}:
        source_categories = {
            proposal.source_category
            for proposal in proposals
            if proposal.source_category != "other"
        }
        if len(source_categories) > 1:
            raise ValueError(
                "Identity groups cannot mix incompatible entity categories"
            )
    normalized_label = normalize_text(canonical_label)
    if not normalized_label:
        raise ValueError("canonical_label must not be empty")

    normalized_alias_kinds: dict[str, str] = {}
    allowed_alias_kinds = {
        "technical",
        "abbreviation",
        "translation",
        "spelling",
    }
    canonical_surface_alias_kind = str(
        alias_kinds.get(canonical_member_proposal_id) or "translation"
    )
    if canonical_surface_alias_kind not in allowed_alias_kinds:
        raise ValueError(
            f"Unsupported identity alias kind: {canonical_surface_alias_kind}"
        )
    for proposal in proposals:
        alias_kind = str(alias_kinds.get(proposal.proposal_id) or "translation")
        if alias_kind not in allowed_alias_kinds:
            raise ValueError(f"Unsupported identity alias kind: {alias_kind}")
        if proposal.proposal_id == canonical_member_proposal_id:
            continue
        normalized_alias_kinds[proposal.proposal_id] = alias_kind

    member_surfaces = sorted(
        (
            {
                "proposal_id": proposal.proposal_id,
                "display_label": proposal.display_label,
                "normalized_surface": str(
                    proposal.payload.get("normalized_surface")
                    or normalize_surface(proposal.display_label)
                ),
                "source_category": proposal.source_category,
                "is_canonical_candidate": (
                    proposal.proposal_id == canonical_member_proposal_id
                ),
                "alias_kind": (
                    canonical_surface_alias_kind
                    if proposal.proposal_id == canonical_member_proposal_id
                    else normalized_alias_kinds.get(proposal.proposal_id)
                ),
                "cluster_count": proposal.cluster_count,
            }
            for proposal in proposals
        ),
        key=lambda item: (
            not bool(item["is_canonical_candidate"]),
            str(item["display_label"]).casefold(),
            str(item["display_label"]),
        ),
    )

    review_kind = "canonicalization" if len(members) == 1 else "identity_group"
    key = (
        f"normalize:{members[0]}:{normalize_surface(normalized_label)}"
        if review_kind == "canonicalization"
        else "merge:" + ":".join(members)
    )
    payload = {
        "display_label": normalized_label,
        "normalized_surface": normalize_surface(normalized_label),
        "source_category": next(
            proposal.source_category
            for proposal in proposals
            if proposal.proposal_id == canonical_member_proposal_id
        ),
        "proposal_kind": "merge",
        "member_proposal_ids": list(members),
        "member_surfaces": member_surfaces,
        "canonical_member_proposal_id": canonical_member_proposal_id,
        "identity_review_kind": review_kind,
        "canonical_surface_alias_kind": canonical_surface_alias_kind,
        "alias_kinds": normalized_alias_kinds,
        "rationale": normalize_text(rationale),
        "resolver": dict(resolver_metadata or {}),
        "candidate_concept_ids": [],
    }
    proposal_id = _proposal_id("merge", key)
    with _immediate_transaction(connection):
        connection.execute(
            """
            INSERT OR IGNORE INTO concept_proposals (
                concept_proposal_id,
                proposal_kind,
                normalized_candidate_key,
                proposal_payload_json,
                proposal_hash,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, 'merge', ?, ?, ?, NULL, ?)
            """,
            (
                proposal_id,
                key,
                canonical_json(payload),
                content_hash(payload, namespace="wiki-v2-identity-group-proposal"),
                _utc_now(),
            ),
        )
        for member in proposals:
            evidence_id = _stable_id(
                "proposal-evidence",
                {
                    "proposal_id": proposal_id,
                    "member_proposal_id": member.proposal_id,
                },
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO concept_proposal_evidence (
                    concept_proposal_evidence_id,
                    concept_proposal_id,
                    source_lineage_id,
                    card_revision_id,
                    occurrence_version_id,
                    evidence_payload_json,
                    created_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    evidence_id,
                    proposal_id,
                    canonical_json(
                        {
                            "member_proposal_id": member.proposal_id,
                            "display_label": member.display_label,
                            "source_category": member.source_category,
                            "cluster_count": member.cluster_count,
                        }
                    ),
                    _utc_now(),
                ),
            )
    return get_proposal(connection, proposal_id)


def normalize_surface(value: str) -> str:
    """Normalize only technical surface variation, without semantic rewriting."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = (
        text.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .casefold()
    )
    text = re.sub(r"\s+", " ", text).strip(" \t\n\r.,;:!?\"'«»()[]{}")
    # D.P.R.K. and DPRK are a technical spelling variant. This rule does not
    # infer that either form means "North Korea".
    if re.fullmatch(r"(?:[\w]\.){2,}[\w]?\.?", text, flags=re.UNICODE):
        text = text.replace(".", "")
    return text


def scan_registry(
    connection: sqlite3.Connection,
    *,
    min_clusters: int = MIN_PROPOSAL_CLUSTERS,
) -> RegistryScanStats:
    """Discover current structured surfaces and create review-only proposals."""
    if min_clusters < 2:
        raise ValueError("min_clusters must be at least 2")

    evidence = _collect_current_surface_evidence(connection)
    by_candidate: dict[str, list[SurfaceEvidence]] = defaultdict(list)
    all_surfaces: set[str] = set()
    cards_seen: set[str] = set()
    eligible_cards: set[str] = set()
    for item in evidence:
        by_candidate[item.candidate_key].append(item)
        all_surfaces.add(item.normalized_surface)
        cards_seen.add(item.card_revision_id)
        eligible_cards.add(item.card_revision_id)

    cards_seen_count = int(
        connection.execute("SELECT COUNT(*) FROM source_lineage_heads").fetchone()[0]
    )
    aliases_by_surface = _approved_alias_candidates(connection)
    concept_categories = {
        concept.concept_id: (concept.concept_kind, concept.source_category)
        for concept in list_concepts(connection)
    }
    unresolved_surfaces = {
        surface for surface in all_surfaces if not aliases_by_surface.get(surface)
    }

    proposals_created = 0
    evidence_created = 0
    surface_changes = 0
    with _immediate_transaction(connection):
        for surface in sorted(all_surfaces | set(aliases_by_surface)):
            surface_changes += int(
                _advance_surface_head_in_transaction(
                    connection,
                    normalized_surface=surface,
                    candidate_concept_ids=aliases_by_surface.get(surface, ()),
                )
            )

        for candidate_key in sorted(by_candidate):
            items = by_candidate[candidate_key]
            clusters = {item.content_cluster_id for item in items}
            source_families = {item.source_family_id for item in items}
            qualification_rule = _candidate_qualification_rule(
                items,
                distinct_content_clusters=len(clusters),
                distinct_source_families=len(source_families),
                minimum_support=min_clusters,
            )
            if qualification_rule is None:
                continue

            representative = min(
                items,
                key=lambda item: (
                    item.display_surface.casefold(),
                    item.display_surface,
                    item.card_revision_id,
                ),
            )
            candidate_ids = aliases_by_surface.get(representative.normalized_surface, ())
            needs_split = bool(
                candidate_ids
                and representative.proposal_kind == "entity"
                and representative.source_category != "other"
                and all(
                    concept_categories.get(concept_id, ("entity", "other"))[0]
                    == "entity"
                    and concept_categories.get(concept_id, ("entity", "other"))[1]
                    not in {"other", representative.source_category}
                    for concept_id in candidate_ids
                )
            )
            if candidate_ids and not needs_split:
                # The surface already resolves to an approved compatible
                # identity. Role/metonym decisions belong downstream.
                continue

            effective_proposal_kind: ProposalKind = (
                "split" if needs_split else representative.proposal_kind
            )
            proposal_payload = {
                "display_label": representative.display_surface,
                "normalized_surface": representative.normalized_surface,
                "source_category": representative.source_category,
                "proposal_kind": effective_proposal_kind,
                "candidate_concept_ids": list(candidate_ids),
                "threshold": {
                    "minimum_distinct_content_clusters": min_clusters,
                    "observed_distinct_content_clusters": len(clusters),
                    "minimum_distinct_source_families": min_clusters,
                    "observed_distinct_source_families": len(source_families),
                },
                "qualification": {
                    "rule_id": qualification_rule,
                    "primary_youtube_segment_topic": (
                        qualification_rule == "primary_youtube_segment_topic"
                    ),
                },
            }
            proposal_id = _proposal_id(effective_proposal_kind, candidate_key)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO concept_proposals (
                    concept_proposal_id,
                    proposal_kind,
                    normalized_candidate_key,
                    proposal_payload_json,
                    proposal_hash,
                    produced_by_stage_version_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    proposal_id,
                    effective_proposal_kind,
                    candidate_key,
                    canonical_json(proposal_payload),
                    content_hash(
                        proposal_payload,
                        namespace="wiki-v2-concept-proposal",
                    ),
                    _utc_now(),
                ),
            )
            proposals_created += int(cursor.rowcount > 0)

            for item in sorted(
                items,
                key=lambda value: (
                    value.content_cluster_id,
                    value.source_lineage_id,
                    value.card_revision_id,
                    canonical_json(value.source_locator),
                ),
            ):
                evidence_payload = {
                    "normalized_surface": item.normalized_surface,
                    "display_surface": item.display_surface,
                    "source_category": item.source_category,
                    "salience": item.salience,
                    "source_role": item.source_role,
                    "source_kind": item.source_kind,
                    "source_family_id": item.source_family_id,
                    "content_cluster_id": item.content_cluster_id,
                    "source_locator": item.source_locator,
                }
                evidence_id = _stable_id(
                    "proposal-evidence",
                    {
                        "proposal_id": proposal_id,
                        "card_revision_id": item.card_revision_id,
                        "content_cluster_id": item.content_cluster_id,
                        "source_locator": item.source_locator,
                    },
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO concept_proposal_evidence (
                        concept_proposal_evidence_id,
                        concept_proposal_id,
                        source_lineage_id,
                        card_revision_id,
                        occurrence_version_id,
                        evidence_payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        evidence_id,
                        proposal_id,
                        item.source_lineage_id,
                        item.card_revision_id,
                        canonical_json(evidence_payload),
                        _utc_now(),
                    ),
                )
                evidence_created += int(cursor.rowcount > 0)

    return RegistryScanStats(
        cards_seen=cards_seen_count,
        cards_eligible=len(eligible_cards),
        surfaces_seen=len(all_surfaces),
        surfaces_unresolved=len(unresolved_surfaces),
        proposals_created=proposals_created,
        evidence_created=evidence_created,
        surface_revisions_changed=surface_changes,
    )


def create_alias_proposal(
    connection: sqlite3.Connection,
    *,
    display_surface: str,
    target_concept_id: str,
    alias_kind: AliasKind,
    rationale: str,
    evidence: Sequence[Mapping[str, Any]] = (),
    source_proposal_id: str | None = None,
) -> ConceptProposal:
    """Create an explicit review proposal for a semantic identity alias."""
    if alias_kind not in {
        "technical",
        "abbreviation",
        "translation",
        "spelling",
    }:
        raise ValueError(f"Unsupported identity alias kind: {alias_kind}")
    concept = get_concept(connection, target_concept_id)
    source_proposal: ConceptProposal | None = None
    if source_proposal_id is not None:
        source_proposal = get_proposal(connection, source_proposal_id)
        if source_proposal.status != "pending":
            raise ProposalStateError("Alias source proposal must still be pending")
        if source_proposal.proposal_kind not in {"entity", "topic", "split"}:
            raise ProposalStateError("Alias source must be a concept candidate proposal")
        if source_proposal.proposal_kind in {"entity", "topic"}:
            source_kind = (
                "topic" if source_proposal.proposal_kind == "topic" else "entity"
            )
            if source_kind != concept.concept_kind:
                raise ValueError("Identity alias cannot cross concept kinds")
            if source_kind == "entity":
                categories = {
                    category
                    for category in (
                        source_proposal.source_category,
                        concept.source_category,
                    )
                    if category != "other"
                }
                if len(categories) > 1:
                    raise ValueError(
                        "Identity alias cannot cross incompatible entity categories"
                    )
    normalized = normalize_surface(display_surface)
    if not normalized:
        raise ValueError("Alias surface must not be empty")
    key = (
        f"alias:{target_concept_id}:{normalized}:{alias_kind}:"
        f"{source_proposal_id or 'manual'}"
    )
    payload = {
        "display_label": normalize_text(display_surface),
        "normalized_surface": normalized,
        "source_category": concept.source_category,
        "proposal_kind": "alias",
        "target_concept_id": target_concept_id,
        "target_concept_label": concept.canonical_label,
        "alias_kind": alias_kind,
        "rationale": normalize_text(rationale),
        "source_proposal_id": source_proposal_id,
        "candidate_concept_ids": [target_concept_id],
    }
    proposal_id = _proposal_id("alias", key)
    with _immediate_transaction(connection):
        connection.execute(
            """
            INSERT OR IGNORE INTO concept_proposals (
                concept_proposal_id,
                proposal_kind,
                normalized_candidate_key,
                proposal_payload_json,
                proposal_hash,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, 'alias', ?, ?, ?, NULL, ?)
            """,
            (
                proposal_id,
                key,
                canonical_json(payload),
                content_hash(payload, namespace="wiki-v2-alias-proposal"),
                _utc_now(),
            ),
        )
        for ordinal, item in enumerate(evidence):
            evidence_payload = dict(item)
            evidence_id = _stable_id(
                "proposal-evidence",
                {
                    "proposal_id": proposal_id,
                    "ordinal": ordinal,
                    "evidence": evidence_payload,
                },
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO concept_proposal_evidence (
                    concept_proposal_evidence_id,
                    concept_proposal_id,
                    source_lineage_id,
                    card_revision_id,
                    occurrence_version_id,
                    evidence_payload_json,
                    created_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    evidence_id,
                    proposal_id,
                    canonical_json(evidence_payload),
                    _utc_now(),
                ),
            )
        if source_proposal is not None:
            evidence_id = _stable_id(
                "proposal-evidence",
                {
                    "proposal_id": proposal_id,
                    "source_proposal_id": source_proposal.proposal_id,
                },
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO concept_proposal_evidence (
                    concept_proposal_evidence_id,
                    concept_proposal_id,
                    source_lineage_id,
                    card_revision_id,
                    occurrence_version_id,
                    evidence_payload_json,
                    created_at
                ) VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    evidence_id,
                    proposal_id,
                    canonical_json(
                        {
                            "source_proposal_id": source_proposal.proposal_id,
                            "display_label": source_proposal.display_label,
                            "cluster_count": source_proposal.cluster_count,
                        }
                    ),
                    _utc_now(),
                ),
            )
    return get_proposal(connection, proposal_id)


def list_proposals(
    connection: sqlite3.Connection,
    *,
    statuses: Sequence[ProposalStatus] = ("pending",),
) -> tuple[ConceptProposal, ...]:
    """Return proposals with their effective immutable review status."""
    allowed = {"pending", "approved", "rejected", "deferred"}
    requested = tuple(dict.fromkeys(statuses))
    if any(status not in allowed for status in requested):
        raise ValueError(f"Unsupported proposal status in {requested!r}")

    proposals: list[ConceptProposal] = []
    rows = connection.execute(
        """
        SELECT
            proposal.*,
            decision.decision,
            decision.rationale
        FROM concept_proposals AS proposal
        LEFT JOIN concept_proposal_current_decisions AS decision
          ON decision.concept_proposal_id = proposal.concept_proposal_id
        ORDER BY proposal.created_at, proposal.concept_proposal_id
        """
    ).fetchall()
    for row in rows:
        proposal = _proposal_from_row(connection, row)
        if proposal.status in requested and (
            proposal.status != "pending"
            or proposal.source_category not in EXCLUDED_ENTITY_CATEGORIES
        ):
            proposals.append(proposal)
    return tuple(proposals)


def pending_proposal_count(connection: sqlite3.Connection) -> int:
    """Return the number of proposals currently requiring a user decision."""
    return len(list_proposals(connection, statuses=("pending",)))


def get_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> ConceptProposal:
    row = connection.execute(
        """
        SELECT
            proposal.*,
            decision.decision,
            decision.rationale
        FROM concept_proposals AS proposal
        LEFT JOIN concept_proposal_current_decisions AS decision
          ON decision.concept_proposal_id = proposal.concept_proposal_id
        WHERE proposal.concept_proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ProposalNotFoundError(proposal_id)
    return _proposal_from_row(connection, row)


def approve_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    canonical_label: str | None = None,
    concept_kind: ConceptKind | None = None,
    source_category: str | None = None,
    description: str = "",
    target_concept_id: str | None = None,
    rationale: str = "",
) -> ReviewResult:
    """Approve one proposal, creating or extending an approved concept."""
    proposal = get_proposal(connection, proposal_id)
    if proposal.status != "pending":
        raise ProposalStateError(
            f"Proposal {proposal_id} is {proposal.status}; reopen it before approval"
        )

    payload = proposal.payload
    resolved_target = target_concept_id or payload.get("target_concept_id")
    with _immediate_transaction(connection):
        if proposal.proposal_kind == "alias":
            if not resolved_target:
                raise ProposalStateError("Alias approval requires target_concept_id")
            concept = get_concept(connection, str(resolved_target))
            alias_kind = str(payload.get("alias_kind") or "translation")
            _insert_identity_alias(
                connection,
                concept_id=concept.concept_id,
                concept_revision_id=concept.concept_revision_id,
                display_surface=canonical_label or proposal.display_label,
                alias_kind=alias_kind,
            )
            concept_id = concept.concept_id
            source_proposal_id = payload.get("source_proposal_id")
            if source_proposal_id:
                source_proposal = get_proposal(connection, str(source_proposal_id))
                if source_proposal.status != "pending":
                    raise ProposalStateError(
                        "Alias source proposal changed before approval"
                    )
                source_generation = _next_decision_generation(
                    connection,
                    source_proposal.proposal_id,
                )
                connection.execute(
                    """
                    INSERT INTO concept_review_decisions (
                        concept_review_decision_id,
                        concept_proposal_id,
                        decision_generation,
                        decision,
                        created_concept_id,
                        rationale,
                        decided_at
                    ) VALUES (?, ?, ?, 'approved', ?, ?, ?)
                    """,
                    (
                        _stable_id(
                            "concept-review",
                            {
                                "proposal_id": source_proposal.proposal_id,
                                "generation": source_generation,
                                "decision": "approved",
                            },
                        ),
                        source_proposal.proposal_id,
                        source_generation,
                        concept.concept_id,
                        normalize_text(rationale)
                        or f"Approved through alias proposal {proposal.proposal_id}",
                        _utc_now(),
                    ),
                )
        elif proposal.proposal_kind in {"entity", "topic", "split"}:
            if resolved_target:
                concept = get_concept(connection, str(resolved_target))
                _insert_identity_alias(
                    connection,
                    concept_id=concept.concept_id,
                    concept_revision_id=concept.concept_revision_id,
                    display_surface=canonical_label or proposal.display_label,
                    alias_kind="translation",
                )
                concept_id = concept.concept_id
            else:
                resolved_kind: ConceptKind = (
                    concept_kind
                    or ("topic" if proposal.proposal_kind == "topic" else "entity")
                )
                concept = _create_concept_in_transaction(
                    connection,
                    proposal=proposal,
                    canonical_label=canonical_label or proposal.display_label,
                    concept_kind=resolved_kind,
                    source_category=source_category or proposal.source_category,
                    description=description,
                )
                concept_id = concept.concept_id
        elif proposal.proposal_kind == "merge":
            concept = _approve_identity_group_in_transaction(
                connection,
                proposal=proposal,
                target_concept_id=(
                    None if not resolved_target else str(resolved_target)
                ),
                canonical_label=canonical_label,
                description=description,
                source_category=source_category,
                rationale=rationale,
            )
            concept_id = concept.concept_id
        else:  # pragma: no cover - schema constrains the value
            raise ProposalStateError(f"Unsupported proposal kind: {proposal.proposal_kind}")

        generation = _next_decision_generation(connection, proposal_id)
        connection.execute(
            """
            INSERT INTO concept_review_decisions (
                concept_review_decision_id,
                concept_proposal_id,
                decision_generation,
                decision,
                created_concept_id,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, 'approved', ?, ?, ?)
            """,
            (
                _stable_id(
                    "concept-review",
                    {
                        "proposal_id": proposal_id,
                        "generation": generation,
                        "decision": "approved",
                    },
                ),
                proposal_id,
                generation,
                concept_id,
                normalize_text(rationale) or None,
                _utc_now(),
            ),
        )
        _refresh_surface_for_concept_in_transaction(connection, concept_id)

    return ReviewResult(
        proposal_id=proposal_id,
        decision="approved",
        decision_generation=generation,
        concept_id=concept_id,
    )


def reject_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str,
) -> ReviewResult:
    """Reject a pending proposal while preserving it for audit/reopen."""
    return _record_nonapproval_decision(
        connection,
        proposal_id=proposal_id,
        decision="rejected",
        rationale=rationale,
    )


def defer_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str = "",
) -> ReviewResult:
    """Defer a pending proposal without approving it."""
    return _record_nonapproval_decision(
        connection,
        proposal_id=proposal_id,
        decision="deferred",
        rationale=rationale,
    )


def reopen_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str = "",
) -> ReviewResult:
    """Reopen a rejected/deferred proposal as pending."""
    proposal = get_proposal(connection, proposal_id)
    if proposal.status not in {"rejected", "deferred"}:
        raise ProposalStateError(
            f"Only rejected/deferred proposals can be reopened, found {proposal.status}"
        )
    generation = _next_decision_generation(connection, proposal_id)
    with _immediate_transaction(connection):
        connection.execute(
            """
            INSERT INTO concept_review_decisions (
                concept_review_decision_id,
                concept_proposal_id,
                decision_generation,
                decision,
                created_concept_id,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, 'reopened', NULL, ?, ?)
            """,
            (
                _stable_id(
                    "concept-review",
                    {
                        "proposal_id": proposal_id,
                        "generation": generation,
                        "decision": "reopened",
                    },
                ),
                proposal_id,
                generation,
                normalize_text(rationale) or None,
                _utc_now(),
            ),
        )
    return ReviewResult(
        proposal_id=proposal_id,
        decision="reopened",
        decision_generation=generation,
        concept_id=None,
    )


def list_concepts(connection: sqlite3.Connection) -> tuple[ApprovedConcept, ...]:
    rows = connection.execute(
        """
        SELECT
            concept.concept_id,
            concept.concept_kind,
            concept.canonical_key,
            revision.concept_revision_id,
            revision.concept_generation,
            revision.canonical_payload_json
        FROM approved_concepts AS concept
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = concept.current_concept_revision_id
        ORDER BY concept.canonical_key, concept.concept_id
        """
    ).fetchall()
    return tuple(_concept_from_row(connection, row) for row in rows)


def get_concept(
    connection: sqlite3.Connection,
    concept_id: str,
) -> ApprovedConcept:
    row = connection.execute(
        """
        SELECT
            concept.concept_id,
            concept.concept_kind,
            concept.canonical_key,
            revision.concept_revision_id,
            revision.concept_generation,
            revision.canonical_payload_json
        FROM approved_concepts AS concept
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = concept.current_concept_revision_id
        WHERE concept.concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    if row is None:
        raise ConceptNotFoundError(concept_id)
    return _concept_from_row(connection, row)


def update_concept_display(
    connection: sqlite3.Connection,
    concept_id: str,
    *,
    canonical_label: str,
    description: str | None = None,
    source_category: str | None = None,
) -> ApprovedConcept:
    """Create a new immutable display revision while retaining stable identity."""
    current = get_concept(connection, concept_id)
    payload = {
        "canonical_label": normalize_text(canonical_label),
        "description": (
            current.description if description is None else normalize_text(description)
        ),
        "source_category": source_category or current.source_category,
        "concept_kind": current.concept_kind,
        "canonical_key": current.canonical_key,
    }
    if not payload["canonical_label"]:
        raise ValueError("canonical_label must not be empty")

    with _immediate_transaction(connection):
        head = connection.execute(
            """
            SELECT current_concept_revision_id, current_concept_generation
            FROM concept_heads
            WHERE concept_id = ?
            """,
            (concept_id,),
        ).fetchone()
        if head is None:
            raise ConceptNotFoundError(concept_id)
        generation = int(head["current_concept_generation"]) + 1
        revision_id = _stable_id(
            "concept-revision",
            {
                "concept_id": concept_id,
                "generation": generation,
                "payload": payload,
            },
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                concept_id,
                generation,
                content_hash(
                    {
                        "concept_kind": current.concept_kind,
                        "canonical_key": current.canonical_key,
                        "source_category": payload["source_category"],
                    },
                    namespace="wiki-v2-concept-identity",
                ),
                content_hash(
                    {
                        "canonical_label": payload["canonical_label"],
                        "description": payload["description"],
                    },
                    namespace="wiki-v2-concept-display",
                ),
                _current_hierarchy_hash(connection, concept_id),
                canonical_json(payload),
                _utc_now(),
            ),
        )
        cursor = connection.execute(
            """
            UPDATE concept_heads
            SET
                current_concept_revision_id = ?,
                current_concept_generation = ?,
                updated_at = ?
            WHERE concept_id = ?
              AND current_concept_revision_id = ?
              AND current_concept_generation = ?
            """,
            (
                revision_id,
                generation,
                _utc_now(),
                concept_id,
                head["current_concept_revision_id"],
                head["current_concept_generation"],
            ),
        )
        if cursor.rowcount != 1:
            raise RegistryError("Concept head changed concurrently")
        _insert_identity_alias(
            connection,
            concept_id=concept_id,
            concept_revision_id=revision_id,
            display_surface=payload["canonical_label"],
            alias_kind="canonical",
        )
        _refresh_surface_for_concept_in_transaction(connection, concept_id)
    return get_concept(connection, concept_id)


def resolve_surface(
    connection: sqlite3.Connection,
    surface: str,
) -> tuple[ApprovedConcept, ...]:
    """Resolve an identity surface to approved concepts only."""
    normalized = normalize_surface(surface)
    if not normalized:
        return ()
    concept_ids = [
        row["concept_id"]
        for row in connection.execute(
            """
            SELECT DISTINCT alias.concept_id
            FROM identity_aliases AS alias
            JOIN approved_concepts AS approved
              ON approved.concept_id = alias.concept_id
            WHERE alias.normalized_surface = ?
            ORDER BY alias.concept_id
            """,
            (normalized,),
        ).fetchall()
    ]
    return tuple(get_concept(connection, concept_id) for concept_id in concept_ids)


def registry_status(connection: sqlite3.Connection) -> dict[str, int]:
    """Return compact counts used by CLI and reviewer."""
    pending = pending_proposal_count(connection)
    return {
        "approved_concepts": int(
            connection.execute("SELECT COUNT(*) FROM approved_concepts").fetchone()[0]
        ),
        "approved_aliases": int(
            connection.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0]
        ),
        "known_surfaces": int(
            connection.execute("SELECT COUNT(*) FROM surface_heads").fetchone()[0]
        ),
        "pending_proposals": pending,
        "rejected_proposals": len(
            list_proposals(connection, statuses=("rejected",))
        ),
        "deferred_proposals": len(
            list_proposals(connection, statuses=("deferred",))
        ),
    }


def publish_registry_dependencies(
    connection: sqlite3.Connection,
) -> RegistryDependencyStats:
    """Publish narrow data snapshots used by downstream stage bindings."""
    concept_changes = 0
    display_changes = 0
    alias_changes = 0
    surface_changes = 0
    for concept in list_concepts(connection):
        concept_head = get_dependency_head(
            connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key=concept.concept_id,
        )
        published = publish_dependency(
            connection,
            dependency_kind="registry_snapshot",
            dependency_scope_key=concept.concept_id,
            payload={
                "concept_id": concept.concept_id,
                "concept_kind": concept.concept_kind,
                "canonical_key": concept.canonical_key,
                "source_category": concept.source_category,
            },
            expected_version_id=(
                None if concept_head is None else concept_head.dependency_version_id
            ),
            producer_kind="registry",
        )
        concept_changes += int(published.changed)

        display_head = get_dependency_head(
            connection,
            dependency_kind="concept_display_snapshot",
            dependency_scope_key=concept.concept_id,
        )
        published = publish_dependency(
            connection,
            dependency_kind="concept_display_snapshot",
            dependency_scope_key=concept.concept_id,
            payload={
                "concept_id": concept.concept_id,
                "canonical_label": concept.canonical_label,
                "description": concept.description,
            },
            expected_version_id=(
                None
                if display_head is None
                else display_head.dependency_version_id
            ),
            producer_kind="registry",
        )
        display_changes += int(published.changed)

        alias_head = get_dependency_head(
            connection,
            dependency_kind="approved_identity_alias_snapshot",
            dependency_scope_key=concept.concept_id,
        )
        alias_rows = connection.execute(
            """
            SELECT normalized_surface, display_surface, alias_kind
            FROM identity_aliases
            WHERE concept_id = ?
            ORDER BY normalized_surface, alias_kind, display_surface
            """,
            (concept.concept_id,),
        ).fetchall()
        published = publish_dependency(
            connection,
            dependency_kind="approved_identity_alias_snapshot",
            dependency_scope_key=concept.concept_id,
            payload={
                "concept_id": concept.concept_id,
                "aliases": [dict(row) for row in alias_rows],
            },
            expected_version_id=(
                None if alias_head is None else alias_head.dependency_version_id
            ),
            producer_kind="registry",
            unordered_collection_paths=(("aliases",),),
        )
        alias_changes += int(published.changed)

    surface_rows = connection.execute(
        """
        SELECT
            head.normalized_surface,
            head.current_surface_generation,
            revision.candidate_concept_ids_json
        FROM surface_heads AS head
        JOIN surface_revisions AS revision
          ON revision.surface_revision_id = head.current_surface_revision_id
        ORDER BY head.normalized_surface
        """
    ).fetchall()
    for row in surface_rows:
        surface = row["normalized_surface"]
        surface_head = get_dependency_head(
            connection,
            dependency_kind="surface_resolution",
            dependency_scope_key=surface,
        )
        published = publish_dependency(
            connection,
            dependency_kind="surface_resolution",
            dependency_scope_key=surface,
            payload={
                "normalized_surface": surface,
                "surface_generation": int(row["current_surface_generation"]),
                "candidate_concept_ids": json.loads(
                    row["candidate_concept_ids_json"]
                ),
            },
            expected_version_id=(
                None if surface_head is None else surface_head.dependency_version_id
            ),
            producer_kind="registry",
            unordered_collection_paths=(("candidate_concept_ids",),),
        )
        surface_changes += int(published.changed)
    return RegistryDependencyStats(
        concept_snapshots_changed=concept_changes,
        display_snapshots_changed=display_changes,
        alias_snapshots_changed=alias_changes,
        surface_snapshots_changed=surface_changes,
    )


def _collect_current_surface_evidence(
    connection: sqlite3.Connection,
) -> tuple[SurfaceEvidence, ...]:
    rows = connection.execute(
        """
        SELECT
            head.source_lineage_id,
            head.current_card_revision_id AS card_revision_id,
            source.source_kind,
            source.external_key,
            revision.canonical_payload_json AS card_payload_json,
            relation_input.canonical_payload_json AS relation_payload_json,
            eligibility.current_eligible
        FROM source_lineage_heads AS head
        JOIN source_lineages AS source
          ON source.source_lineage_id = head.source_lineage_id
        JOIN card_revisions AS revision
          ON revision.card_revision_id = head.current_card_revision_id
         AND revision.source_lineage_id = head.source_lineage_id
        JOIN card_revision_input_bindings AS binding
          ON binding.card_revision_id = head.current_card_revision_id
         AND binding.source_lineage_id = head.source_lineage_id
         AND binding.input_kind = 'structured_relation_inputs'
        JOIN lineage_input_versions AS relation_input
          ON relation_input.input_version_id = binding.input_version_id
        JOIN eligibility_heads AS eligibility
          ON eligibility.source_lineage_id = head.source_lineage_id
         AND eligibility.evaluated_card_revision_id = head.current_card_revision_id
        JOIN active_processor_contract_heads AS contract_head
          ON contract_head.stage_kind = eligibility.stage_kind
         AND contract_head.current_activation_generation =
             eligibility.current_processor_contract_activation_generation
        ORDER BY head.source_lineage_id
        """
    ).fetchall()
    prepared_rows = tuple(
        (row, json.loads(row["card_payload_json"]))
        for row in rows
    )
    source_family_ids = _source_family_ids(prepared_rows)
    evidence: list[SurfaceEvidence] = []
    for row, _card_payload in prepared_rows:
        if not bool(row["current_eligible"]):
            continue
        cluster_id = _content_cluster_id(connection, row["source_lineage_id"])
        if cluster_id is None:
            continue
        source_family_id = source_family_ids[row["source_lineage_id"]]
        payload = json.loads(row["relation_payload_json"])
        entities = payload.get("entities") or {}
        if isinstance(entities, Mapping):
            for category in ENTITY_CATEGORIES:
                if category in EXCLUDED_ENTITY_CATEGORIES:
                    continue
                values = entities.get(category) or []
                if not isinstance(values, list):
                    continue
                for ordinal, item in enumerate(values):
                    if not isinstance(item, Mapping):
                        continue
                    display = normalize_text(str(item.get("text") or ""))
                    normalized = normalize_surface(display)
                    if not normalized:
                        continue
                    evidence.append(
                        SurfaceEvidence(
                            normalized_surface=normalized,
                            display_surface=display,
                            proposal_kind="entity",
                            candidate_key=f"entity:{category}:{normalized}",
                            source_category=category,
                            salience=str(item.get("salience") or "mentioned"),
                            source_role=str(item.get("role") or ""),
                            source_kind=str(row["source_kind"]),
                            source_family_id=source_family_id,
                            source_lineage_id=row["source_lineage_id"],
                            card_revision_id=row["card_revision_id"],
                            content_cluster_id=cluster_id,
                            source_locator={
                                "field": "entities",
                                "category": category,
                                "content_fingerprint": content_hash(
                                    dict(item),
                                    namespace="wiki-v2-structured-surface",
                                ),
                                "diagnostic_index": ordinal,
                            },
                        )
                    )
        topics = payload.get("topics") or []
        if isinstance(topics, list):
            for ordinal, item in enumerate(topics):
                if not isinstance(item, Mapping):
                    continue
                display = normalize_text(str(item.get("label") or ""))
                normalized = normalize_surface(display)
                if not normalized:
                    continue
                topic_type = str(item.get("type") or "other")
                evidence.append(
                    SurfaceEvidence(
                        normalized_surface=normalized,
                        display_surface=display,
                        proposal_kind="topic",
                        candidate_key=f"topic:{topic_type}:{normalized}",
                        source_category=topic_type,
                        salience=str(item.get("salience") or "mentioned"),
                        source_role="topic",
                        source_kind=str(row["source_kind"]),
                        source_family_id=source_family_id,
                        source_lineage_id=row["source_lineage_id"],
                        card_revision_id=row["card_revision_id"],
                        content_cluster_id=cluster_id,
                        source_locator={
                            "field": "topics",
                            "topic_type": topic_type,
                            "content_fingerprint": content_hash(
                                dict(item),
                                namespace="wiki-v2-structured-surface",
                            ),
                            "diagnostic_index": ordinal,
                        },
                    )
                )
    return tuple(evidence)


def _candidate_qualification_rule(
    items: Sequence[SurfaceEvidence],
    *,
    distinct_content_clusters: int,
    distinct_source_families: int,
    minimum_support: int,
) -> str | None:
    if (
        distinct_content_clusters >= minimum_support
        and distinct_source_families >= minimum_support
    ):
        return "independent_source_families"
    if any(
        item.proposal_kind == "topic"
        and item.salience == "primary"
        and item.source_kind == "youtube_segment"
        for item in items
    ):
        return "primary_youtube_segment_topic"
    return None


def _source_family_ids(
    rows: Sequence[tuple[sqlite3.Row, Mapping[str, Any]]],
) -> dict[str, str]:
    parent_by_external_key: dict[str, str] = {}
    external_by_lineage: dict[str, str] = {}
    for row, card_payload in rows:
        external_key = normalize_text(str(row["external_key"]))
        external_by_lineage[str(row["source_lineage_id"])] = external_key
        if str(row["source_kind"]) not in {"youtube", "youtube_segment"}:
            continue
        source = card_payload.get("source") or {}
        if not isinstance(source, Mapping):
            continue
        parent_source_id = normalize_text(
            str(source.get("parent_source_id") or "")
        )
        if parent_source_id and parent_source_id != external_key:
            parent_by_external_key[external_key] = parent_source_id

    root_cache: dict[str, str] = {}

    def resolve_root(external_key: str) -> str:
        trail: list[str] = []
        current = external_key
        while True:
            cached = root_cache.get(current)
            if cached is not None:
                root = cached
                break
            if current in trail:
                cycle = trail[trail.index(current) :]
                root = min(cycle)
                break
            trail.append(current)
            parent = parent_by_external_key.get(current)
            if parent is None:
                root = current
                break
            current = parent
        for member in trail:
            root_cache[member] = root
        return root

    return {
        lineage_id: content_hash(
            {"source_family_root": resolve_root(external_key)},
            namespace="wiki-v2-source-family",
        )
        for lineage_id, external_key in external_by_lineage.items()
    }


def _content_cluster_id(
    connection: sqlite3.Connection,
    source_lineage_id: str,
) -> str | None:
    rows = connection.execute(
        """
        SELECT occurrence.exact_payload_hash
        FROM effective_active_occurrences AS occurrence
        WHERE occurrence.source_lineage_id = ?
        ORDER BY occurrence.exact_payload_hash, occurrence.occurrence_version_id
        """,
        (source_lineage_id,),
    ).fetchall()
    hashes = sorted({row["exact_payload_hash"] for row in rows})
    if not hashes:
        return None
    return content_hash(
        {"active_occurrence_payload_hashes": hashes},
        namespace="wiki-v2-substantive-content-cluster",
        unordered_collection_paths=(("active_occurrence_payload_hashes",),),
    )


def _approved_alias_candidates(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT alias.normalized_surface, alias.concept_id
        FROM identity_aliases AS alias
        JOIN approved_concepts AS concept ON concept.concept_id = alias.concept_id
        ORDER BY alias.normalized_surface, alias.concept_id
        """
    ).fetchall()
    for row in rows:
        if row["concept_id"] not in grouped[row["normalized_surface"]]:
            grouped[row["normalized_surface"]].append(row["concept_id"])
    return {surface: tuple(concepts) for surface, concepts in grouped.items()}


def _proposal_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> ConceptProposal:
    payload = json.loads(row["proposal_payload_json"])
    decision = row["decision"]
    status: ProposalStatus
    if decision == "approved":
        status = "approved"
    elif decision == "rejected":
        status = "rejected"
    elif decision == "deferred":
        status = "deferred"
    else:
        status = "pending"
    evidence_rows = connection.execute(
        """
        SELECT evidence_payload_json
        FROM concept_proposal_evidence
        WHERE concept_proposal_id = ?
        """,
        (row["concept_proposal_id"],),
    ).fetchall()
    clusters: set[str] = set()
    for evidence_row in evidence_rows:
        evidence_payload = json.loads(evidence_row["evidence_payload_json"])
        cluster = evidence_payload.get("content_cluster_id")
        if cluster:
            clusters.add(str(cluster))
    return ConceptProposal(
        proposal_id=row["concept_proposal_id"],
        proposal_kind=row["proposal_kind"],
        normalized_candidate_key=row["normalized_candidate_key"],
        display_label=str(
            payload.get("display_label")
            or payload.get("normalized_surface")
            or row["normalized_candidate_key"]
        ),
        source_category=str(payload.get("source_category") or "other"),
        status=status,
        evidence_count=len(evidence_rows),
        cluster_count=len(clusters),
        candidate_concept_ids=tuple(
            str(value) for value in payload.get("candidate_concept_ids", [])
        ),
        payload=payload,
        created_at=row["created_at"],
        latest_rationale=row["rationale"],
    )


def _approve_identity_group_in_transaction(
    connection: sqlite3.Connection,
    *,
    proposal: ConceptProposal,
    target_concept_id: str | None,
    canonical_label: str | None,
    description: str,
    source_category: str | None,
    rationale: str,
) -> ApprovedConcept:
    payload = proposal.payload
    member_ids = tuple(
        sorted({str(value) for value in payload.get("member_proposal_ids", [])})
    )
    canonical_member_id = str(payload.get("canonical_member_proposal_id") or "")
    if not member_ids or canonical_member_id not in member_ids:
        raise ProposalStateError("Identity-group proposal payload is invalid")
    members = [get_proposal(connection, member_id) for member_id in member_ids]
    if any(member.status != "pending" for member in members):
        raise ProposalStateError(
            "Every identity-group member must remain pending at approval time"
        )
    canonical_member = next(
        member for member in members if member.proposal_id == canonical_member_id
    )

    if target_concept_id is not None:
        concept = get_concept(connection, target_concept_id)
    else:
        concept = _create_concept_in_transaction(
            connection,
            proposal=canonical_member,
            canonical_label=(
                canonical_label
                or str(payload.get("display_label") or canonical_member.display_label)
            ),
            concept_kind=(
                "topic" if canonical_member.proposal_kind == "topic" else "entity"
            ),
            source_category=(
                source_category
                or str(payload.get("source_category") or canonical_member.source_category)
            ),
            description=description,
            source_alias_kind=str(
                payload.get("canonical_surface_alias_kind") or "translation"
            ),
        )

    alias_kinds = payload.get("alias_kinds") or {}
    for member in members:
        alias_kind = (
            str(payload.get("canonical_surface_alias_kind") or "translation")
            if member.proposal_id == canonical_member_id
            else str(alias_kinds.get(member.proposal_id) or "translation")
        )
        if normalize_surface(member.display_label) == normalize_surface(
            concept.canonical_label
        ):
            alias_kind = "canonical"
        _insert_identity_alias(
            connection,
            concept_id=concept.concept_id,
            concept_revision_id=concept.concept_revision_id,
            display_surface=member.display_label,
            alias_kind=alias_kind,
        )
        generation = _next_decision_generation(connection, member.proposal_id)
        connection.execute(
            """
            INSERT INTO concept_review_decisions (
                concept_review_decision_id,
                concept_proposal_id,
                decision_generation,
                decision,
                created_concept_id,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, 'approved', ?, ?, ?)
            """,
            (
                _stable_id(
                    "concept-review",
                    {
                        "proposal_id": member.proposal_id,
                        "generation": generation,
                        "decision": "approved",
                    },
                ),
                member.proposal_id,
                generation,
                concept.concept_id,
                normalize_text(rationale)
                or f"Approved through identity group {proposal.proposal_id}",
                _utc_now(),
            ),
        )
    _refresh_surface_for_concept_in_transaction(connection, concept.concept_id)
    return get_concept(connection, concept.concept_id)


def _create_concept_in_transaction(
    connection: sqlite3.Connection,
    *,
    proposal: ConceptProposal,
    canonical_label: str,
    concept_kind: ConceptKind,
    source_category: str,
    description: str,
    source_alias_kind: str = "translation",
) -> ApprovedConcept:
    label = normalize_text(canonical_label)
    if not label:
        raise ValueError("canonical_label must not be empty")
    canonical_key = proposal.normalized_candidate_key
    existing = connection.execute(
        "SELECT concept_id FROM concepts WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    if existing is not None:
        return get_concept(connection, existing["concept_id"])

    concept_id = _stable_id(
        "concept",
        {
            "proposal_id": proposal.proposal_id,
            "canonical_key": canonical_key,
        },
    )
    payload = {
        "canonical_label": label,
        "description": normalize_text(description),
        "source_category": source_category or "other",
        "concept_kind": concept_kind,
        "canonical_key": canonical_key,
    }
    revision_id = _stable_id(
        "concept-revision",
        {
            "concept_id": concept_id,
            "generation": 1,
            "payload": payload,
        },
    )
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO concepts (
            concept_id,
            concept_kind,
            approval_status,
            canonical_key,
            created_at
        ) VALUES (?, ?, 'approved', ?, ?)
        """,
        (concept_id, concept_kind, canonical_key, now),
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
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            concept_id,
            content_hash(
                {
                    "concept_kind": concept_kind,
                    "canonical_key": canonical_key,
                    "source_category": payload["source_category"],
                },
                namespace="wiki-v2-concept-identity",
            ),
            content_hash(
                {
                    "canonical_label": label,
                    "description": payload["description"],
                },
                namespace="wiki-v2-concept-display",
            ),
            content_hash({}, namespace="wiki-v2-concept-hierarchy"),
            canonical_json(payload),
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO concept_heads (
            concept_id,
            current_concept_revision_id,
            current_concept_generation,
            updated_at
        ) VALUES (?, ?, 1, ?)
        """,
        (concept_id, revision_id, now),
    )
    _insert_identity_alias(
        connection,
        concept_id=concept_id,
        concept_revision_id=revision_id,
        display_surface=label,
        alias_kind="canonical",
    )
    if normalize_surface(proposal.display_label) != normalize_surface(label):
        _insert_identity_alias(
            connection,
            concept_id=concept_id,
            concept_revision_id=revision_id,
            display_surface=proposal.display_label,
            alias_kind=source_alias_kind,
        )
    return get_concept(connection, concept_id)


def _insert_identity_alias(
    connection: sqlite3.Connection,
    *,
    concept_id: str,
    concept_revision_id: str,
    display_surface: str,
    alias_kind: str,
) -> None:
    if alias_kind not in {
        "canonical",
        "technical",
        "abbreviation",
        "translation",
        "spelling",
    }:
        raise ValueError(f"Unsupported identity alias kind: {alias_kind}")
    display = normalize_text(display_surface)
    normalized = normalize_surface(display)
    if not normalized:
        raise ValueError("Identity alias surface must not be empty")
    alias_id = _stable_id(
        "identity-alias",
        {
            "concept_id": concept_id,
            "normalized_surface": normalized,
            "alias_kind": alias_kind,
        },
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO identity_aliases (
            identity_alias_id,
            concept_id,
            concept_revision_id,
            normalized_surface,
            display_surface,
            alias_kind,
            approved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alias_id,
            concept_id,
            concept_revision_id,
            normalized,
            display,
            alias_kind,
            _utc_now(),
        ),
    )


def _record_nonapproval_decision(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    decision: Literal["rejected", "deferred"],
    rationale: str,
) -> ReviewResult:
    proposal = get_proposal(connection, proposal_id)
    if proposal.status != "pending":
        raise ProposalStateError(
            f"Proposal {proposal_id} is {proposal.status}; it is not pending"
        )
    normalized_rationale = normalize_text(rationale)
    if decision == "rejected" and not normalized_rationale:
        raise ValueError("A rejected proposal requires a rationale")
    generation = _next_decision_generation(connection, proposal_id)
    with _immediate_transaction(connection):
        connection.execute(
            """
            INSERT INTO concept_review_decisions (
                concept_review_decision_id,
                concept_proposal_id,
                decision_generation,
                decision,
                created_concept_id,
                rationale,
                decided_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                _stable_id(
                    "concept-review",
                    {
                        "proposal_id": proposal_id,
                        "generation": generation,
                        "decision": decision,
                    },
                ),
                proposal_id,
                generation,
                decision,
                normalized_rationale or None,
                _utc_now(),
            ),
        )
    return ReviewResult(
        proposal_id=proposal_id,
        decision=decision,
        decision_generation=generation,
        concept_id=None,
    )


def _next_decision_generation(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(decision_generation), 0) + 1 AS next_generation
        FROM concept_review_decisions
        WHERE concept_proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    return int(row["next_generation"])


def _concept_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> ApprovedConcept:
    payload = json.loads(row["canonical_payload_json"])
    aliases = tuple(
        alias_row["display_surface"]
        for alias_row in connection.execute(
            """
            SELECT display_surface
            FROM identity_aliases
            WHERE concept_id = ?
            ORDER BY
                CASE alias_kind
                    WHEN 'canonical' THEN 0
                    WHEN 'abbreviation' THEN 1
                    WHEN 'translation' THEN 2
                    ELSE 3
                END,
                display_surface COLLATE NOCASE,
                display_surface
            """,
            (row["concept_id"],),
        ).fetchall()
    )
    return ApprovedConcept(
        concept_id=row["concept_id"],
        concept_kind=row["concept_kind"],
        canonical_key=row["canonical_key"],
        canonical_label=str(payload.get("canonical_label") or row["canonical_key"]),
        description=str(payload.get("description") or ""),
        source_category=str(payload.get("source_category") or "other"),
        concept_revision_id=row["concept_revision_id"],
        generation=int(row["concept_generation"]),
        aliases=aliases,
    )


def _refresh_surface_for_concept_in_transaction(
    connection: sqlite3.Connection,
    concept_id: str,
) -> None:
    surfaces = [
        row["normalized_surface"]
        for row in connection.execute(
            """
            SELECT DISTINCT normalized_surface
            FROM identity_aliases
            WHERE concept_id = ?
            ORDER BY normalized_surface
            """,
            (concept_id,),
        ).fetchall()
    ]
    for surface in surfaces:
        candidates = tuple(
            row["concept_id"]
            for row in connection.execute(
                """
                SELECT DISTINCT alias.concept_id
                FROM identity_aliases AS alias
                JOIN approved_concepts AS concept
                  ON concept.concept_id = alias.concept_id
                WHERE alias.normalized_surface = ?
                ORDER BY alias.concept_id
                """,
                (surface,),
            ).fetchall()
        )
        _advance_surface_head_in_transaction(
            connection,
            normalized_surface=surface,
            candidate_concept_ids=candidates,
        )


def _advance_surface_head_in_transaction(
    connection: sqlite3.Connection,
    *,
    normalized_surface: str,
    candidate_concept_ids: Sequence[str],
) -> bool:
    candidates = tuple(sorted(set(candidate_concept_ids)))
    payload_json = canonical_json(list(candidates))
    resolution_hash = content_hash(
        {"candidate_concept_ids": list(candidates)},
        namespace=f"wiki-v2-surface-resolution:{normalized_surface}",
        unordered_collection_paths=(("candidate_concept_ids",),),
    )
    current = connection.execute(
        """
        SELECT
            head.current_surface_revision_id,
            head.current_surface_generation,
            revision.surface_resolution_hash
        FROM surface_heads AS head
        JOIN surface_revisions AS revision
          ON revision.surface_revision_id = head.current_surface_revision_id
        WHERE head.normalized_surface = ?
        """,
        (normalized_surface,),
    ).fetchone()
    if current is not None and current["surface_resolution_hash"] == resolution_hash:
        return False

    generation = 1 if current is None else int(current["current_surface_generation"]) + 1
    revision_id = _stable_id(
        "surface-revision",
        {
            "normalized_surface": normalized_surface,
            "generation": generation,
            "resolution_hash": resolution_hash,
        },
    )
    now = _utc_now()
    connection.execute(
        """
        INSERT INTO surface_revisions (
            surface_revision_id,
            normalized_surface,
            surface_generation,
            surface_resolution_hash,
            candidate_concept_ids_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            normalized_surface,
            generation,
            resolution_hash,
            payload_json,
            now,
        ),
    )
    if current is None:
        connection.execute(
            """
            INSERT INTO surface_heads (
                normalized_surface,
                current_surface_revision_id,
                current_surface_generation,
                updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (normalized_surface, revision_id, generation, now),
        )
    else:
        cursor = connection.execute(
            """
            UPDATE surface_heads
            SET
                current_surface_revision_id = ?,
                current_surface_generation = ?,
                updated_at = ?
            WHERE normalized_surface = ?
              AND current_surface_revision_id = ?
              AND current_surface_generation = ?
            """,
            (
                revision_id,
                generation,
                now,
                normalized_surface,
                current["current_surface_revision_id"],
                current["current_surface_generation"],
            ),
        )
        if cursor.rowcount != 1:
            raise RegistryError(f"Surface head {normalized_surface!r} changed concurrently")
    return True


def _current_hierarchy_hash(
    connection: sqlite3.Connection,
    concept_id: str,
) -> str:
    primary = connection.execute(
        """
        SELECT parent_concept_id
        FROM effective_primary_hierarchy_edges
        WHERE child_concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    related = [
        row["related_concept_id"]
        for row in connection.execute(
            """
            SELECT
                CASE
                    WHEN left_concept_id = ? THEN right_concept_id
                    ELSE left_concept_id
                END AS related_concept_id
            FROM effective_related_concept_edges
            WHERE left_concept_id = ? OR right_concept_id = ?
            ORDER BY related_concept_id
            """,
            (concept_id, concept_id, concept_id),
        ).fetchall()
    ]
    return content_hash(
        {
            "primary_parent": None if primary is None else primary["parent_concept_id"],
            "related_concepts": related,
        },
        namespace="wiki-v2-concept-hierarchy",
        unordered_collection_paths=(("related_concepts",),),
    )


def _proposal_id(proposal_kind: str, candidate_key: str) -> str:
    return _stable_id(
        "concept-proposal",
        {"proposal_kind": proposal_kind, "candidate_key": candidate_key},
    )


def _stable_id(prefix: str, payload: Any) -> str:
    digest = sha256_hex(
        f"{prefix}\n{canonical_json(payload)}"
    )
    return f"{prefix}:v1:sha256:{digest}"


@contextmanager
def _immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise RegistryError("Registry operations require an outermost transaction")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
