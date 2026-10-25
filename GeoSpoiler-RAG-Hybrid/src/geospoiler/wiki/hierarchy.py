"""Review-gated concept hierarchy and bounded related-topic graph."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from geospoiler.wiki.hashing import canonical_json, content_hash, normalize_text, sha256_hex
from geospoiler.wiki.registry import ConceptNotFoundError, get_concept
from geospoiler.wiki.state import _immediate_transaction, get_dependency_head, publish_dependency

HierarchyEdgeKind = Literal["primary_parent", "related"]
HierarchyStatus = Literal["pending", "approved", "rejected", "deferred"]

MAX_RELATED_CONCEPTS = 5


class HierarchyError(RuntimeError):
    """Base hierarchy error."""


class HierarchyCycleError(HierarchyError):
    """Raised when a primary-parent decision would create a cycle."""


class HierarchyLimitError(HierarchyError):
    """Raised when a related edge would exceed the bounded graph."""


class HierarchyProposalStateError(HierarchyError):
    """Raised for an invalid review transition."""


@dataclass(frozen=True)
class HierarchyProposal:
    proposal_id: str
    child_concept_id: str
    other_concept_id: str
    edge_kind: HierarchyEdgeKind
    status: HierarchyStatus
    rationale: str
    payload: dict[str, Any]
    created_at: str
    latest_rationale: str | None


@dataclass(frozen=True)
class HierarchyReviewResult:
    proposal_id: str
    decision: Literal["approved", "rejected", "deferred", "reopened"]
    decision_generation: int


@dataclass(frozen=True)
class ConceptTreeNode:
    concept_id: str
    canonical_label: str
    concept_kind: str
    parent_concept_id: str | None
    related_concept_ids: tuple[str, ...]
    child_concept_ids: tuple[str, ...]


def create_hierarchy_proposal(
    connection: sqlite3.Connection,
    *,
    child_concept_id: str,
    other_concept_id: str,
    edge_kind: HierarchyEdgeKind,
    rationale: str,
    resolver_metadata: Mapping[str, Any] | None = None,
    batch_id: str | None = None,
) -> HierarchyProposal:
    """Create a proposal only; no hierarchy edge is changed."""
    if child_concept_id == other_concept_id:
        raise ValueError("Hierarchy concepts must be different")
    get_concept(connection, child_concept_id)
    get_concept(connection, other_concept_id)
    if edge_kind not in {"primary_parent", "related"}:
        raise ValueError(f"Unsupported hierarchy edge kind: {edge_kind}")
    payload = {
        "child_concept_id": child_concept_id,
        "parent_or_related_concept_id": other_concept_id,
        "edge_kind": edge_kind,
        "rationale": normalize_text(rationale),
        "resolver": dict(resolver_metadata or {}),
        "batch_id": batch_id,
    }
    proposal_id = _stable_id(
        "hierarchy-proposal",
        {
            "child_concept_id": child_concept_id,
            "other_concept_id": other_concept_id,
            "edge_kind": edge_kind,
        },
    )
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(proposal_generation), 0) + 1
                FROM hierarchy_proposals
                WHERE child_concept_id = ?
                  AND parent_or_related_concept_id = ?
                  AND edge_kind = ?
                """,
                (child_concept_id, other_concept_id, edge_kind),
            ).fetchone()[0]
        )
        # A stable proposal ID makes reruns idempotent. Rejected proposals are
        # reopened explicitly, not silently recreated by a later model run.
        connection.execute(
            """
            INSERT OR IGNORE INTO hierarchy_proposals (
                hierarchy_proposal_id,
                child_concept_id,
                parent_or_related_concept_id,
                edge_kind,
                proposal_generation,
                proposal_payload_json,
                produced_by_stage_version_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                proposal_id,
                child_concept_id,
                other_concept_id,
                edge_kind,
                generation,
                canonical_json(payload),
                _utc_now(),
            ),
        )
    return get_hierarchy_proposal(connection, proposal_id)


def list_hierarchy_proposals(
    connection: sqlite3.Connection,
    *,
    statuses: Sequence[HierarchyStatus] = ("pending",),
) -> tuple[HierarchyProposal, ...]:
    allowed = {"pending", "approved", "rejected", "deferred"}
    requested = tuple(dict.fromkeys(statuses))
    if any(status not in allowed for status in requested):
        raise ValueError(f"Unsupported hierarchy status in {requested!r}")
    rows = connection.execute(
        """
        SELECT
            proposal.*,
            decision.decision,
            decision.rationale AS latest_rationale
        FROM hierarchy_proposals AS proposal
        LEFT JOIN hierarchy_proposal_current_decisions AS decision
          ON decision.hierarchy_proposal_id = proposal.hierarchy_proposal_id
        ORDER BY proposal.created_at, proposal.hierarchy_proposal_id
        """
    ).fetchall()
    proposals = [_hierarchy_proposal_from_row(row) for row in rows]
    return tuple(
        proposal for proposal in proposals if proposal.status in requested
    )


def get_hierarchy_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> HierarchyProposal:
    row = connection.execute(
        """
        SELECT
            proposal.*,
            decision.decision,
            decision.rationale AS latest_rationale
        FROM hierarchy_proposals AS proposal
        LEFT JOIN hierarchy_proposal_current_decisions AS decision
          ON decision.hierarchy_proposal_id = proposal.hierarchy_proposal_id
        WHERE proposal.hierarchy_proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise HierarchyError(f"Unknown hierarchy proposal {proposal_id}")
    return _hierarchy_proposal_from_row(row)


def approve_hierarchy_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str = "",
) -> HierarchyReviewResult:
    """Apply one explicitly approved edge and preserve its review history."""
    proposal = get_hierarchy_proposal(connection, proposal_id)
    if proposal.status != "pending":
        raise HierarchyProposalStateError(
            f"Hierarchy proposal is {proposal.status}; reopen it before approval"
        )
    with _immediate_transaction(connection):
        _require_pending_in_transaction(connection, proposal_id)
        decision_generation = _next_review_generation(connection, proposal_id)
        decision_id = _insert_review_decision(
            connection,
            proposal_id=proposal_id,
            generation=decision_generation,
            decision="approved",
            rationale=rationale,
        )
        touched: set[str]
        if proposal.edge_kind == "primary_parent":
            previous_parent = connection.execute(
                """
                SELECT parent_concept_id
                FROM effective_primary_hierarchy_edges
                WHERE child_concept_id = ?
                """,
                (proposal.child_concept_id,),
            ).fetchone()
            _assert_no_primary_cycle(
                connection,
                child_concept_id=proposal.child_concept_id,
                parent_concept_id=proposal.other_concept_id,
            )
            generation = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(approval_generation), 0) + 1
                    FROM approved_primary_hierarchy_edges
                    WHERE child_concept_id = ?
                    """,
                    (proposal.child_concept_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO approved_primary_hierarchy_edges (
                    primary_hierarchy_edge_id,
                    child_concept_id,
                    parent_concept_id,
                    approval_generation,
                    action,
                    review_decision_id,
                    hierarchy_review_decision_id,
                    approved_at
                ) VALUES (?, ?, ?, ?, 'approve', NULL, ?, ?)
                """,
                (
                    _stable_id(
                        "primary-hierarchy-edge",
                        {
                            "child_concept_id": proposal.child_concept_id,
                            "generation": generation,
                            "parent_concept_id": proposal.other_concept_id,
                        },
                    ),
                    proposal.child_concept_id,
                    proposal.other_concept_id,
                    generation,
                    decision_id,
                    _utc_now(),
                ),
            )
            touched = {
                proposal.child_concept_id,
                proposal.other_concept_id,
            }
            if previous_parent is not None:
                touched.add(previous_parent["parent_concept_id"])
        else:
            left, right = sorted(
                (proposal.child_concept_id, proposal.other_concept_id)
            )
            _assert_related_capacity(connection, left, right)
            generation = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(approval_generation), 0) + 1
                    FROM approved_related_concept_edges
                    WHERE left_concept_id = ? AND right_concept_id = ?
                    """,
                    (left, right),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO approved_related_concept_edges (
                    related_concept_edge_id,
                    left_concept_id,
                    right_concept_id,
                    approval_generation,
                    action,
                    review_decision_id,
                    hierarchy_review_decision_id,
                    approved_at
                ) VALUES (?, ?, ?, ?, 'approve', NULL, ?, ?)
                """,
                (
                    _stable_id(
                        "related-concept-edge",
                        {
                            "left_concept_id": left,
                            "right_concept_id": right,
                            "generation": generation,
                            "action": "approve",
                        },
                    ),
                    left,
                    right,
                    generation,
                    decision_id,
                    _utc_now(),
                ),
            )
            touched = {left, right}
        for concept_id in sorted(touched):
            _refresh_concept_hierarchy_revision(connection, concept_id)
    publish_hierarchy_dependencies(connection, concept_ids=sorted(touched))
    return HierarchyReviewResult(
        proposal_id=proposal_id,
        decision="approved",
        decision_generation=decision_generation,
    )


def reject_hierarchy_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str,
) -> HierarchyReviewResult:
    if not normalize_text(rationale):
        raise ValueError("A rejected hierarchy proposal requires a rationale")
    return _record_nonapproval(
        connection,
        proposal_id=proposal_id,
        decision="rejected",
        rationale=rationale,
    )


def defer_hierarchy_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str = "",
) -> HierarchyReviewResult:
    return _record_nonapproval(
        connection,
        proposal_id=proposal_id,
        decision="deferred",
        rationale=rationale,
    )


def review_hierarchy_batch(
    connection: sqlite3.Connection,
    proposal_ids: Sequence[str],
    *,
    decision: Literal["approve", "reject"],
    rationale: str,
) -> tuple[HierarchyReviewResult, ...]:
    """Apply a reviewer batch atomically so no partial tree can escape."""
    normalized_ids = tuple(dict.fromkeys(proposal_ids))
    if not normalized_ids:
        return ()
    if decision not in {"approve", "reject"}:
        raise ValueError(f"Unsupported hierarchy batch decision: {decision}")
    with _immediate_transaction(connection):
        if decision == "approve":
            return tuple(
                approve_hierarchy_proposal(
                    connection,
                    proposal_id,
                    rationale=rationale,
                )
                for proposal_id in normalized_ids
            )
        return tuple(
            reject_hierarchy_proposal(
                connection,
                proposal_id,
                rationale=rationale,
            )
            for proposal_id in normalized_ids
        )


def reopen_hierarchy_proposal(
    connection: sqlite3.Connection,
    proposal_id: str,
    *,
    rationale: str = "",
) -> HierarchyReviewResult:
    proposal = get_hierarchy_proposal(connection, proposal_id)
    if proposal.status not in {"rejected", "deferred"}:
        raise HierarchyProposalStateError(
            f"Only rejected/deferred hierarchy proposals can reopen; found {proposal.status}"
        )
    with _immediate_transaction(connection):
        generation = _next_review_generation(connection, proposal_id)
        _insert_review_decision(
            connection,
            proposal_id=proposal_id,
            generation=generation,
            decision="reopened",
            rationale=rationale,
        )
    return HierarchyReviewResult(
        proposal_id=proposal_id,
        decision="reopened",
        decision_generation=generation,
    )


def remove_primary_parent(
    connection: sqlite3.Connection,
    child_concept_id: str,
) -> None:
    """Explicitly remove a primary parent; never called by automatic analysis."""
    get_concept(connection, child_concept_id)
    current = connection.execute(
        """
        SELECT parent_concept_id
        FROM effective_primary_hierarchy_edges
        WHERE child_concept_id = ?
        """,
        (child_concept_id,),
    ).fetchone()
    if current is None:
        return
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(approval_generation), 0) + 1
                FROM approved_primary_hierarchy_edges
                WHERE child_concept_id = ?
                """,
                (child_concept_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO approved_primary_hierarchy_edges (
                primary_hierarchy_edge_id,
                child_concept_id,
                parent_concept_id,
                approval_generation,
                action,
                review_decision_id,
                hierarchy_review_decision_id,
                approved_at
            ) VALUES (?, ?, NULL, ?, 'remove', NULL, NULL, ?)
            """,
            (
                _stable_id(
                    "primary-hierarchy-edge",
                    {
                        "child_concept_id": child_concept_id,
                        "generation": generation,
                        "action": "remove",
                    },
                ),
                child_concept_id,
                generation,
                _utc_now(),
            ),
        )
        _refresh_concept_hierarchy_revision(connection, child_concept_id)
        _refresh_concept_hierarchy_revision(
            connection, current["parent_concept_id"]
        )
    publish_hierarchy_dependencies(
        connection,
        concept_ids=(child_concept_id, current["parent_concept_id"]),
    )


def remove_related_edge(
    connection: sqlite3.Connection,
    left_concept_id: str,
    right_concept_id: str,
) -> None:
    """Explicitly remove one approved related edge."""
    left, right = sorted((left_concept_id, right_concept_id))
    if left == right:
        raise ValueError("Related concepts must be different")
    effective = connection.execute(
        """
        SELECT 1
        FROM effective_related_concept_edges
        WHERE left_concept_id = ? AND right_concept_id = ?
        """,
        (left, right),
    ).fetchone()
    if effective is None:
        return
    with _immediate_transaction(connection):
        generation = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(approval_generation), 0) + 1
                FROM approved_related_concept_edges
                WHERE left_concept_id = ? AND right_concept_id = ?
                """,
                (left, right),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO approved_related_concept_edges (
                related_concept_edge_id,
                left_concept_id,
                right_concept_id,
                approval_generation,
                action,
                review_decision_id,
                hierarchy_review_decision_id,
                approved_at
            ) VALUES (?, ?, ?, ?, 'remove', NULL, NULL, ?)
            """,
            (
                _stable_id(
                    "related-concept-edge",
                    {
                        "left_concept_id": left,
                        "right_concept_id": right,
                        "generation": generation,
                        "action": "remove",
                    },
                ),
                left,
                right,
                generation,
                _utc_now(),
            ),
        )
        _refresh_concept_hierarchy_revision(connection, left)
        _refresh_concept_hierarchy_revision(connection, right)
    publish_hierarchy_dependencies(connection, concept_ids=(left, right))


def list_concept_tree(connection: sqlite3.Connection) -> tuple[ConceptTreeNode, ...]:
    concepts = connection.execute(
        """
        SELECT
            concept.concept_id,
            concept.concept_kind,
            revision.canonical_payload_json
        FROM approved_concepts AS concept
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = concept.current_concept_revision_id
        ORDER BY concept.concept_id
        """
    ).fetchall()
    parents = {
        row["child_concept_id"]: row["parent_concept_id"]
        for row in connection.execute(
            """
            SELECT child_concept_id, parent_concept_id
            FROM effective_primary_hierarchy_edges
            """
        ).fetchall()
    }
    children: dict[str, list[str]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    related: dict[str, list[str]] = {}
    for row in connection.execute(
        """
        SELECT left_concept_id, right_concept_id
        FROM effective_related_concept_edges
        """
    ).fetchall():
        related.setdefault(row["left_concept_id"], []).append(
            row["right_concept_id"]
        )
        related.setdefault(row["right_concept_id"], []).append(
            row["left_concept_id"]
        )
    return tuple(
        ConceptTreeNode(
            concept_id=row["concept_id"],
            canonical_label=str(
                json.loads(row["canonical_payload_json"]).get("canonical_label")
                or row["concept_id"]
            ),
            concept_kind=row["concept_kind"],
            parent_concept_id=parents.get(row["concept_id"]),
            related_concept_ids=tuple(
                sorted(related.get(row["concept_id"], ()))
            ),
            child_concept_ids=tuple(
                sorted(children.get(row["concept_id"], ()))
            ),
        )
        for row in concepts
    )


def pending_hierarchy_count(connection: sqlite3.Connection) -> int:
    return len(list_hierarchy_proposals(connection, statuses=("pending",)))


def publish_hierarchy_dependencies(
    connection: sqlite3.Connection,
    *,
    concept_ids: Sequence[str] | None = None,
) -> int:
    """Publish per-concept hierarchy data without invalidating identity stages."""
    resolved_ids = (
        [
            row["concept_id"]
            for row in connection.execute(
                "SELECT concept_id FROM approved_concepts ORDER BY concept_id"
            ).fetchall()
        ]
        if concept_ids is None
        else sorted(set(concept_ids))
    )
    changed = 0
    for concept_id in resolved_ids:
        get_concept(connection, concept_id)
        current = get_dependency_head(
            connection,
            dependency_kind="hierarchy_snapshot",
            dependency_scope_key=concept_id,
        )
        published = publish_dependency(
            connection,
            dependency_kind="hierarchy_snapshot",
            dependency_scope_key=concept_id,
            payload={
                "concept_id": concept_id,
                **_hierarchy_payload(connection, concept_id),
            },
            expected_version_id=(
                None if current is None else current.dependency_version_id
            ),
            producer_kind="manual",
            unordered_collection_paths=(
                ("children",),
                ("related_concepts",),
            ),
        )
        changed += int(published.changed)
    return changed


def _record_nonapproval(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    decision: Literal["rejected", "deferred"],
    rationale: str,
) -> HierarchyReviewResult:
    proposal = get_hierarchy_proposal(connection, proposal_id)
    if proposal.status != "pending":
        raise HierarchyProposalStateError(
            f"Hierarchy proposal is {proposal.status}; it is not pending"
        )
    with _immediate_transaction(connection):
        _require_pending_in_transaction(connection, proposal_id)
        generation = _next_review_generation(connection, proposal_id)
        _insert_review_decision(
            connection,
            proposal_id=proposal_id,
            generation=generation,
            decision=decision,
            rationale=rationale,
        )
    return HierarchyReviewResult(
        proposal_id=proposal_id,
        decision=decision,
        decision_generation=generation,
    )


def _hierarchy_proposal_from_row(row: sqlite3.Row) -> HierarchyProposal:
    payload = json.loads(row["proposal_payload_json"])
    decision = row["decision"]
    status: HierarchyStatus
    if decision == "approved":
        status = "approved"
    elif decision == "rejected":
        status = "rejected"
    elif decision == "deferred":
        status = "deferred"
    else:
        status = "pending"
    return HierarchyProposal(
        proposal_id=row["hierarchy_proposal_id"],
        child_concept_id=row["child_concept_id"],
        other_concept_id=row["parent_or_related_concept_id"],
        edge_kind=row["edge_kind"],
        status=status,
        rationale=str(payload.get("rationale") or ""),
        payload=payload,
        created_at=row["created_at"],
        latest_rationale=row["latest_rationale"],
    )


def _require_pending_in_transaction(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT decision
        FROM hierarchy_proposal_current_decisions
        WHERE hierarchy_proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is not None and row["decision"] != "reopened":
        raise HierarchyProposalStateError(
            f"Hierarchy proposal is already {row['decision']}"
        )


def _next_review_generation(
    connection: sqlite3.Connection,
    proposal_id: str,
) -> int:
    return int(
        connection.execute(
            """
            SELECT COALESCE(MAX(decision_generation), 0) + 1
            FROM hierarchy_review_decisions
            WHERE hierarchy_proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()[0]
    )


def _insert_review_decision(
    connection: sqlite3.Connection,
    *,
    proposal_id: str,
    generation: int,
    decision: Literal["approved", "rejected", "deferred", "reopened"],
    rationale: str,
) -> str:
    decision_id = _stable_id(
        "hierarchy-review",
        {
            "proposal_id": proposal_id,
            "generation": generation,
            "decision": decision,
        },
    )
    connection.execute(
        """
        INSERT INTO hierarchy_review_decisions (
            hierarchy_review_decision_id,
            hierarchy_proposal_id,
            decision_generation,
            decision,
            rationale,
            decided_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            proposal_id,
            generation,
            decision,
            normalize_text(rationale) or None,
            _utc_now(),
        ),
    )
    return decision_id


def _assert_no_primary_cycle(
    connection: sqlite3.Connection,
    *,
    child_concept_id: str,
    parent_concept_id: str,
) -> None:
    if child_concept_id == parent_concept_id:
        raise HierarchyCycleError("A concept cannot be its own parent")
    parent_by_child = {
        row["child_concept_id"]: row["parent_concept_id"]
        for row in connection.execute(
            """
            SELECT child_concept_id, parent_concept_id
            FROM effective_primary_hierarchy_edges
            """
        ).fetchall()
    }
    parent_by_child[child_concept_id] = parent_concept_id
    seen: set[str] = set()
    current: str | None = child_concept_id
    while current is not None:
        if current in seen:
            raise HierarchyCycleError(
                f"Primary-parent edge would create a cycle at {current}"
            )
        seen.add(current)
        current = parent_by_child.get(current)


def _assert_related_capacity(
    connection: sqlite3.Connection,
    left_concept_id: str,
    right_concept_id: str,
) -> None:
    exists = connection.execute(
        """
        SELECT 1
        FROM effective_related_concept_edges
        WHERE left_concept_id = ? AND right_concept_id = ?
        """,
        (left_concept_id, right_concept_id),
    ).fetchone()
    if exists is not None:
        return
    for concept_id in (left_concept_id, right_concept_id):
        count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM effective_related_concept_edges
                WHERE left_concept_id = ? OR right_concept_id = ?
                """,
                (concept_id, concept_id),
            ).fetchone()[0]
        )
        if count >= MAX_RELATED_CONCEPTS:
            raise HierarchyLimitError(
                f"Concept {concept_id} already has {count} related concepts"
            )


def _refresh_concept_hierarchy_revision(
    connection: sqlite3.Connection,
    concept_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT
            head.current_concept_revision_id,
            head.current_concept_generation,
            revision.identity_hash,
            revision.display_hash,
            revision.canonical_payload_json
        FROM concept_heads AS head
        JOIN concept_revisions AS revision
          ON revision.concept_revision_id = head.current_concept_revision_id
        WHERE head.concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    if row is None:
        raise ConceptNotFoundError(concept_id)
    hierarchy_payload = _hierarchy_payload(connection, concept_id)
    hierarchy_hash = content_hash(
        hierarchy_payload,
        namespace="wiki-v2-concept-hierarchy",
        unordered_collection_paths=(
            ("children",),
            ("related_concepts",),
        ),
    )
    current_hash = connection.execute(
        """
        SELECT hierarchy_hash
        FROM concept_revisions
        WHERE concept_revision_id = ?
        """,
        (row["current_concept_revision_id"],),
    ).fetchone()[0]
    if current_hash == hierarchy_hash:
        return
    generation = int(row["current_concept_generation"]) + 1
    revision_id = _stable_id(
        "concept-revision",
        {
            "concept_id": concept_id,
            "generation": generation,
            "canonical_payload_json": row["canonical_payload_json"],
            "hierarchy_hash": hierarchy_hash,
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
            row["identity_hash"],
            row["display_hash"],
            hierarchy_hash,
            row["canonical_payload_json"],
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
            row["current_concept_revision_id"],
            row["current_concept_generation"],
        ),
    )
    if cursor.rowcount != 1:
        raise HierarchyError("Concept head changed while applying hierarchy")


def _hierarchy_payload(
    connection: sqlite3.Connection,
    concept_id: str,
) -> dict[str, Any]:
    parent = connection.execute(
        """
        SELECT parent_concept_id
        FROM effective_primary_hierarchy_edges
        WHERE child_concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    children = [
        row["child_concept_id"]
        for row in connection.execute(
            """
            SELECT child_concept_id
            FROM effective_primary_hierarchy_edges
            WHERE parent_concept_id = ?
            ORDER BY child_concept_id
            """,
            (concept_id,),
        ).fetchall()
    ]
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
    return {
        "primary_parent": None if parent is None else parent["parent_concept_id"],
        "children": children,
        "related_concepts": related,
    }


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}:v1:sha256:{sha256_hex(prefix + chr(10) + canonical_json(payload))}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
