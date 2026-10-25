"""Optional Luna analysis that creates review proposals, never registry facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from geospoiler.providers import llm_backend
from geospoiler.wiki.hashing import canonical_json, content_hash, normalize_text, sha256_hex
from geospoiler.wiki.hierarchy import create_hierarchy_proposal, list_concept_tree
from geospoiler.wiki.registry import (
    ConceptProposal,
    create_alias_proposal,
    create_identity_group_proposal,
    list_concepts,
    list_proposals,
    normalize_surface,
)

IDENTITY_REVIEW_PROMPT_VERSION = "wiki-identity-review-v3"
HIERARCHY_REVIEW_PROMPT_VERSION = "wiki-hierarchy-review-v1"
_ALIAS_KINDS = {"technical", "abbreviation", "translation", "spelling"}
_CANONICALIZATION_ALIAS_KINDS = {"technical", "spelling"}
_IDENTITY_CONTEXT_FIELDS = (
    ("events", "description", "event", 5),
    ("key_points", "text", "key_point", 4),
    ("theses", "text", "thesis", 3),
    ("quotes", "text", "quote", 2),
)


@dataclass(frozen=True)
class IdentityAnalysisStats:
    candidates_sent: int
    concepts_sent: int
    identity_groups_created: int
    canonicalizations_created: int
    alias_proposals_created: int
    skipped_suggestions: int
    model_profile: str
    error: str | None = None
    cache_hit: bool = False


@dataclass(frozen=True)
class HierarchyAnalysisStats:
    concepts_sent: int
    primary_proposals_created: int
    related_proposals_created: int
    skipped_suggestions: int
    model_profile: str
    batch_id: str | None
    error: str | None = None
    cache_hit: bool = False


def propose_identity_reviews_with_luna(
    connection,
    *,
    limit: int = 200,
) -> IdentityAnalysisStats:
    """Ask Luna for strict identity equivalence and persist only proposals.

    The model may pair surfaces, but it cannot write concepts, aliases, or
    decisions. Every accepted suggestion is still shown to the user.
    """
    pending = [
        proposal
        for proposal in list_proposals(connection, statuses=("pending",))
        if proposal.proposal_kind in {"entity", "topic"}
    ][: max(0, limit)]
    concepts = list(list_concepts(connection))
    model_profile = llm_backend.active_model_for("default")
    if not pending:
        return IdentityAnalysisStats(
            candidates_sent=0,
            concepts_sent=len(concepts),
            identity_groups_created=0,
            canonicalizations_created=0,
            alias_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
        )
    if not llm_backend.is_luna_role("default"):
        return IdentityAnalysisStats(
            candidates_sent=len(pending),
            concepts_sent=len(concepts),
            identity_groups_created=0,
            canonicalizations_created=0,
            alias_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            error="Luna profile is not active; deterministic proposals were kept for review",
        )

    candidate_payload = [
        {
            "proposal_id": proposal.proposal_id,
            "kind": proposal.proposal_kind,
            "label": proposal.display_label,
            "normalized_key": proposal.normalized_candidate_key,
            "source_category": proposal.source_category,
            "independent_clusters": proposal.cluster_count,
            "independent_source_families": int(
                (proposal.payload.get("threshold") or {}).get(
                    "observed_distinct_source_families"
                )
                or proposal.cluster_count
            ),
            "evidence_examples": _identity_evidence_examples(
                connection,
                proposal,
            ),
        }
        for proposal in pending
    ]
    concept_payload = [
        {
            "concept_id": concept.concept_id,
            "kind": concept.concept_kind,
            "canonical_label": concept.canonical_label,
            "source_category": concept.source_category,
            "aliases": list(concept.aliases),
        }
        for concept in concepts
    ]
    analysis_inputs_hash = _analysis_inputs_hash(
        analysis_kind="identity_review",
        model_profile=model_profile,
        prompt_template_version=IDENTITY_REVIEW_PROMPT_VERSION,
        payload={
            "pending_candidates": candidate_payload,
            "approved_concepts": concept_payload,
        },
    )
    if _load_analysis_artifact(
        connection,
        analysis_kind="identity_review",
        analysis_inputs_hash=analysis_inputs_hash,
    ) is not None:
        return IdentityAnalysisStats(
            candidates_sent=len(pending),
            concepts_sent=len(concepts),
            identity_groups_created=0,
            canonicalizations_created=0,
            alias_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            cache_hit=True,
        )
    prompt = f"""
Ты проверяешь ТОЛЬКО тождество Wiki-сущностей и тем GeoSpoiler.

Объединяй формы, если они обозначают один и тот же устойчивый concept:
- грамматические формы и падежи: Европа / Европе / Европы,
  Украина / Украины / Украиной, Россия / России;
- регистр, пунктуацию и другие технические варианты;
- настоящие аббревиатуры: КНДР / DPRK;
- настоящие переводы и общеупотребительные эквиваленты:
  КНДР / Северная Корея / DPRK;
- явные варианты написания или опечатки.

canonical_label всегда пиши в нормальной словарной форме: Европа, Украина,
Россия, Северная Корея. Для грамматических форм, регистра и пунктуации используй
alias_kind=technical; для сокращений — abbreviation; для переводов — translation;
для вариантов написания и опечаток — spelling.

Evidence examples показывают, как форма употреблена в исходных claims/events.
Используй их для различения омонимов. Нельзя объединять:
- город, штат, страну, правительство и лидера только из-за похожего названия;
- столицу с государством (Москва != Россия, Пхеньян != КНДР);
- организацию с человеком;
- метонимы, связанные concepts и более широкие/узкие темы;
- омонимы, если контекст не доказывает тождество.

Если сомневаешься — ничего не предлагай. Не придумывай aliases и IDs. Все
proposal_id/concept_id копируй дословно из JSON-входа. Один pending proposal
не должен встречаться больше одного раза.

Pending candidates (JSON):
{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}

Approved concepts (JSON):
{json.dumps(concept_payload, ensure_ascii=False, indent=2)}

Верни:
1) identity_groups — группы минимум из двух pending proposals, являющихся одним
concept;
2) aliases_to_existing — pending proposal, являющийся alias уже approved concept.
3) canonicalizations — один pending proposal, чьё display-название стоит привести
к нормальной словарной форме, даже если других форм этого concept во входе нет:
например, Европы → Европа или Украиной → Украина. Не возвращай canonicalization,
если label уже находится в нормальной форме. Canonicalization не создаёт перевод
или сокращение: alias_kind здесь может быть только technical или spelling.
Каждый результат остаётся только предложением для ручного review.
""".strip()
    try:
        response = llm_backend.complete_json_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "Return conservative identity-equivalence proposals only. "
                        "Contextual metonymy is forbidden."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            role="default",
            schema=_identity_schema(),
        )
    except Exception as exc:
        return IdentityAnalysisStats(
            candidates_sent=len(pending),
            concepts_sent=len(concepts),
            identity_groups_created=0,
            canonicalizations_created=0,
            alias_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            error=str(exc),
        )

    proposal_by_id = {proposal.proposal_id: proposal for proposal in pending}
    concept_by_id = {concept.concept_id: concept for concept in concepts}
    consumed: set[str] = set()
    groups_created = 0
    canonicalizations_created = 0
    aliases_created = 0
    skipped = 0
    metadata = {
        "prompt_template_version": IDENTITY_REVIEW_PROMPT_VERSION,
        "model_profile_version": model_profile,
        "resolver_kind": "luna_identity_equivalence",
    }

    for suggestion in response.get("identity_groups", []):
        validated = _validate_group(suggestion, proposal_by_id, consumed)
        if validated is None:
            skipped += 1
            continue
        members, canonical_id, canonical_label, alias_kinds, reason = validated
        try:
            create_identity_group_proposal(
                connection,
                member_proposal_ids=members,
                canonical_member_proposal_id=canonical_id,
                canonical_label=canonical_label,
                alias_kinds=alias_kinds,
                rationale=reason,
                resolver_metadata=metadata,
            )
        except Exception:
            skipped += 1
            continue
        consumed.update(members)
        groups_created += 1

    for suggestion in response.get("aliases_to_existing", []):
        proposal_id = str(suggestion.get("proposal_id") or "")
        target_id = str(suggestion.get("target_concept_id") or "")
        alias_kind = str(suggestion.get("alias_kind") or "")
        reason = str(suggestion.get("reason") or "").strip()
        proposal = proposal_by_id.get(proposal_id)
        target = concept_by_id.get(target_id)
        if (
            proposal is None
            or proposal_id in consumed
            or target is None
            or alias_kind not in _ALIAS_KINDS
            or not reason
            or not _proposal_matches_concept(proposal, target)
        ):
            skipped += 1
            continue
        try:
            create_alias_proposal(
                connection,
                display_surface=proposal.display_label,
                target_concept_id=target_id,
                alias_kind=alias_kind,  # type: ignore[arg-type]
                rationale=reason,
                source_proposal_id=proposal_id,
                evidence=(
                    {
                        "resolver": metadata,
                        "source_proposal_id": proposal_id,
                    },
                ),
            )
        except Exception:
            skipped += 1
            continue
        consumed.add(proposal_id)
        aliases_created += 1

    for suggestion in response.get("canonicalizations", []):
        validated = _validate_canonicalization(
            suggestion,
            proposal_by_id,
            consumed,
        )
        if validated is None:
            skipped += 1
            continue
        proposal_id, canonical_label, alias_kind, reason = validated
        try:
            create_identity_group_proposal(
                connection,
                member_proposal_ids=(proposal_id,),
                canonical_member_proposal_id=proposal_id,
                canonical_label=canonical_label,
                alias_kinds={proposal_id: alias_kind},
                rationale=reason,
                resolver_metadata=metadata,
            )
        except Exception:
            skipped += 1
            continue
        consumed.add(proposal_id)
        canonicalizations_created += 1

    _store_analysis_artifact(
        connection,
        analysis_kind="identity_review",
        analysis_inputs_hash=analysis_inputs_hash,
        model_profile=model_profile,
        prompt_template_version=IDENTITY_REVIEW_PROMPT_VERSION,
        response=response,
    )
    return IdentityAnalysisStats(
        candidates_sent=len(pending),
        concepts_sent=len(concepts),
        identity_groups_created=groups_created,
        canonicalizations_created=canonicalizations_created,
        alias_proposals_created=aliases_created,
        skipped_suggestions=skipped,
        model_profile=model_profile,
    )


def propose_hierarchy_reviews_with_luna(
    connection,
) -> HierarchyAnalysisStats:
    """Ask Luna for an orientation tree while keeping every edge review-gated."""
    nodes = list(list_concept_tree(connection))
    model_profile = llm_backend.active_model_for("default")
    if len(nodes) < 2:
        return HierarchyAnalysisStats(
            concepts_sent=len(nodes),
            primary_proposals_created=0,
            related_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            batch_id=None,
        )
    if not llm_backend.is_luna_role("default"):
        return HierarchyAnalysisStats(
            concepts_sent=len(nodes),
            primary_proposals_created=0,
            related_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            batch_id=None,
            error="Luna profile is not active; hierarchy was not guessed",
        )
    concepts = list_concepts(connection)
    concept_by_id = {concept.concept_id: concept for concept in concepts}
    tree_payload = [
        {
            "concept_id": node.concept_id,
            "canonical_label": node.canonical_label,
            "kind": node.concept_kind,
            "source_category": concept_by_id[node.concept_id].source_category,
            "current_parent_concept_id": node.parent_concept_id,
            "current_related_concept_ids": list(node.related_concept_ids),
        }
        for node in nodes
    ]
    analysis_inputs_hash = _analysis_inputs_hash(
        analysis_kind="hierarchy_review",
        model_profile=model_profile,
        prompt_template_version=HIERARCHY_REVIEW_PROMPT_VERSION,
        payload={"concepts_and_current_tree": tree_payload},
    )
    if _load_analysis_artifact(
        connection,
        analysis_kind="hierarchy_review",
        analysis_inputs_hash=analysis_inputs_hash,
    ) is not None:
        return HierarchyAnalysisStats(
            concepts_sent=len(nodes),
            primary_proposals_created=0,
            related_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            batch_id=None,
            cache_hit=True,
        )
    prompt = f"""
Построй консервативные предложения по навигации Wiki только между уже approved
concepts ниже.

Правила:
- не создавай synthetic parents и новые IDs;
- primary_parent означает реальную устойчивую иерархию «частный concept входит
  в более широкий concept», а не просто тематическую связь;
- у concept может быть только один primary parent;
- существующую ветку предлагай менять только при явной серьёзной ошибке;
- related — максимум 3–5 действительно важных соседних concepts;
- не дублируй primary связь как related;
- если уверенности нет, пропусти связь.

Concepts and current tree:
{tree_payload}

Скопируй concept_id дословно. Ответ содержит только proposals; ни одна связь
не будет применена без пользователя.
""".strip()
    try:
        response = llm_backend.complete_json_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "Propose a sparse, stable hierarchy among provided IDs only. "
                        "Never invent concepts."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            role="default",
            schema=_hierarchy_schema(),
        )
    except Exception as exc:
        return HierarchyAnalysisStats(
            concepts_sent=len(nodes),
            primary_proposals_created=0,
            related_proposals_created=0,
            skipped_suggestions=0,
            model_profile=model_profile,
            batch_id=None,
            error=str(exc),
        )

    batch_payload = {
        "prompt_template_version": HIERARCHY_REVIEW_PROMPT_VERSION,
        "model_profile_version": model_profile,
        "response": response,
    }
    batch_id = (
        "hierarchy-batch:"
        + content_hash(
            batch_payload,
            namespace="wiki-v2-hierarchy-proposal-batch",
        ).removeprefix("sha256:")
    )
    resolver_metadata = {
        "prompt_template_version": HIERARCHY_REVIEW_PROMPT_VERSION,
        "model_profile_version": model_profile,
        "resolver_kind": "luna_hierarchy_proposer",
    }
    valid_ids = set(concept_by_id)
    used_children: set[str] = set()
    proposed_primary = 0
    proposed_related = 0
    skipped = 0
    for raw in response.get("primary_edges", []):
        child_id = str(raw.get("child_concept_id") or "")
        parent_id = str(raw.get("parent_concept_id") or "")
        reason = str(raw.get("reason") or "").strip()
        if (
            child_id not in valid_ids
            or parent_id not in valid_ids
            or child_id == parent_id
            or child_id in used_children
            or not reason
        ):
            skipped += 1
            continue
        try:
            create_hierarchy_proposal(
                connection,
                child_concept_id=child_id,
                other_concept_id=parent_id,
                edge_kind="primary_parent",
                rationale=reason,
                resolver_metadata={
                    **resolver_metadata,
                    "current_parent_concept_id": next(
                        node.parent_concept_id
                        for node in nodes
                        if node.concept_id == child_id
                    ),
                },
                batch_id=batch_id,
            )
        except Exception:
            skipped += 1
            continue
        used_children.add(child_id)
        proposed_primary += 1

    seen_related: set[tuple[str, str]] = set()
    for raw in response.get("related_edges", []):
        left_id = str(raw.get("left_concept_id") or "")
        right_id = str(raw.get("right_concept_id") or "")
        reason = str(raw.get("reason") or "").strip()
        pair = tuple(sorted((left_id, right_id)))
        if (
            left_id not in valid_ids
            or right_id not in valid_ids
            or left_id == right_id
            or pair in seen_related
            or not reason
        ):
            skipped += 1
            continue
        try:
            create_hierarchy_proposal(
                connection,
                child_concept_id=pair[0],
                other_concept_id=pair[1],
                edge_kind="related",
                rationale=reason,
                resolver_metadata=resolver_metadata,
                batch_id=batch_id,
            )
        except Exception:
            skipped += 1
            continue
        seen_related.add(pair)
        proposed_related += 1
    _store_analysis_artifact(
        connection,
        analysis_kind="hierarchy_review",
        analysis_inputs_hash=analysis_inputs_hash,
        model_profile=model_profile,
        prompt_template_version=HIERARCHY_REVIEW_PROMPT_VERSION,
        response=response,
    )
    return HierarchyAnalysisStats(
        concepts_sent=len(nodes),
        primary_proposals_created=proposed_primary,
        related_proposals_created=proposed_related,
        skipped_suggestions=skipped,
        model_profile=model_profile,
        batch_id=batch_id,
    )


def _analysis_inputs_hash(
    *,
    analysis_kind: str,
    model_profile: str,
    prompt_template_version: str,
    payload: object,
) -> str:
    return content_hash(
        {
            "analysis_kind": analysis_kind,
            "model_profile_version": model_profile,
            "prompt_template_version": prompt_template_version,
            "payload": payload,
        },
        namespace="wiki-v2-llm-analysis-inputs",
    )


def _load_analysis_artifact(
    connection,
    *,
    analysis_kind: str,
    analysis_inputs_hash: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT response_json
        FROM llm_analysis_artifacts
        WHERE analysis_kind = ?
          AND analysis_inputs_hash = ?
        """,
        (analysis_kind, analysis_inputs_hash),
    ).fetchone()
    if row is None:
        return None
    response = json.loads(row["response_json"])
    return response if isinstance(response, dict) else None


def _store_analysis_artifact(
    connection,
    *,
    analysis_kind: str,
    analysis_inputs_hash: str,
    model_profile: str,
    prompt_template_version: str,
    response: dict[str, Any],
) -> None:
    artifact_id = (
        "llm-analysis:v1:sha256:"
        + sha256_hex(
            canonical_json(
                {
                    "analysis_kind": analysis_kind,
                    "analysis_inputs_hash": analysis_inputs_hash,
                }
            )
        )
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO llm_analysis_artifacts (
            llm_analysis_artifact_id,
            analysis_kind,
            analysis_inputs_hash,
            model_profile_version,
            prompt_template_version,
            response_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (
            artifact_id,
            analysis_kind,
            analysis_inputs_hash,
            model_profile,
            prompt_template_version,
            canonical_json(response),
        ),
    )


def _validate_group(
    suggestion: Any,
    proposal_by_id: dict[str, ConceptProposal],
    consumed: set[str],
) -> tuple[tuple[str, ...], str, str, dict[str, str], str] | None:
    if not isinstance(suggestion, dict):
        return None
    raw_members = suggestion.get("members")
    if not isinstance(raw_members, list):
        return None
    member_ids: list[str] = []
    alias_kinds: dict[str, str] = {}
    for raw_member in raw_members:
        if not isinstance(raw_member, dict):
            return None
        proposal_id = str(raw_member.get("proposal_id") or "")
        alias_kind = str(raw_member.get("alias_kind") or "")
        if (
            proposal_id not in proposal_by_id
            or proposal_id in member_ids
            or proposal_id in consumed
            or alias_kind not in _ALIAS_KINDS
        ):
            return None
        member_ids.append(proposal_id)
        alias_kinds[proposal_id] = alias_kind
    if len(member_ids) < 2:
        return None
    canonical_id = str(suggestion.get("canonical_member_proposal_id") or "")
    canonical_label = str(suggestion.get("canonical_label") or "").strip()
    reason = str(suggestion.get("reason") or "").strip()
    if canonical_id not in member_ids or not canonical_label or not reason:
        return None
    kinds = {
        proposal_by_id[proposal_id].proposal_kind
        for proposal_id in member_ids
    }
    if len(kinds) != 1:
        return None
    if kinds == {"entity"}:
        categories = {
            proposal_by_id[proposal_id].source_category
            for proposal_id in member_ids
            if proposal_by_id[proposal_id].source_category != "other"
        }
        if len(categories) > 1:
            return None
    alias_kinds.pop(canonical_id, None)
    return tuple(sorted(member_ids)), canonical_id, canonical_label, alias_kinds, reason


def _proposal_matches_concept(proposal: ConceptProposal, concept: Any) -> bool:
    proposal_kind = "topic" if proposal.proposal_kind == "topic" else "entity"
    if proposal_kind != concept.concept_kind:
        return False
    if proposal_kind == "topic":
        return True
    categories = {
        category
        for category in (proposal.source_category, concept.source_category)
        if category != "other"
    }
    return len(categories) <= 1


def _validate_canonicalization(
    suggestion: Any,
    proposal_by_id: dict[str, ConceptProposal],
    consumed: set[str],
) -> tuple[str, str, str, str] | None:
    if not isinstance(suggestion, dict):
        return None
    proposal_id = str(suggestion.get("proposal_id") or "")
    canonical_label = normalize_text(str(suggestion.get("canonical_label") or ""))
    alias_kind = str(suggestion.get("alias_kind") or "")
    reason = normalize_text(str(suggestion.get("reason") or ""))
    proposal = proposal_by_id.get(proposal_id)
    if (
        proposal is None
        or proposal_id in consumed
        or not canonical_label
        or canonical_label == normalize_text(proposal.display_label)
        or alias_kind not in _CANONICALIZATION_ALIAS_KINDS
        or not reason
    ):
        return None
    return proposal_id, canonical_label, alias_kind, reason


def _identity_evidence_examples(
    connection,
    proposal: ConceptProposal,
    *,
    limit: int = 2,
) -> list[dict[str, str]]:
    rows = connection.execute(
        """
        SELECT
            evidence.evidence_payload_json,
            source.source_kind,
            revision.canonical_payload_json
        FROM concept_proposal_evidence AS evidence
        JOIN source_lineages AS source
          ON source.source_lineage_id = evidence.source_lineage_id
        JOIN card_revisions AS revision
          ON revision.card_revision_id = evidence.card_revision_id
         AND revision.source_lineage_id = evidence.source_lineage_id
        WHERE evidence.concept_proposal_id = ?
        ORDER BY
            evidence.card_revision_id,
            evidence.concept_proposal_evidence_id
        """,
        (proposal.proposal_id,),
    ).fetchall()
    by_family: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}
    for row in rows:
        evidence = json.loads(row["evidence_payload_json"])
        card_payload = json.loads(row["canonical_payload_json"])
        surface = str(
            evidence.get("normalized_surface")
            or proposal.payload.get("normalized_surface")
            or proposal.display_label
        )
        snippet = _identity_context_snippet(card_payload, surface)
        family_id = str(evidence.get("source_family_id") or "")
        if not family_id:
            continue
        example = {
            "source_family_id": family_id,
            "source_kind": str(row["source_kind"]),
            "source_role": str(evidence.get("source_role") or ""),
            "salience": str(evidence.get("salience") or ""),
            "field_kind": snippet["field_kind"],
            "text": snippet["text"],
        }
        rank = (
            int(normalize_surface(surface) in normalize_surface(snippet["text"])),
            int(str(row["source_kind"]) == "youtube_segment"),
        )
        current = by_family.get(family_id)
        if current is None or rank > current[0]:
            by_family[family_id] = (rank, example)
    return [
        value[1]
        for _, value in sorted(
            by_family.items(),
            key=lambda item: item[0],
        )[:limit]
    ]


def _identity_context_snippet(
    card_payload: Mapping[str, Any],
    surface: str,
) -> dict[str, str]:
    normalized_surface = normalize_surface(surface)
    candidates: list[tuple[tuple[int, int, int], dict[str, str]]] = []
    for field, text_key, field_kind, priority in _IDENTITY_CONTEXT_FIELDS:
        values = card_payload.get(field) or []
        if not isinstance(values, list):
            continue
        for ordinal, item in enumerate(values):
            if not isinstance(item, Mapping):
                continue
            text = str(item.get(text_key) or "").strip()
            if not text:
                continue
            candidates.append(
                (
                    (
                        int(normalized_surface in normalize_surface(text)),
                        priority,
                        -ordinal,
                    ),
                    {
                        "field_kind": field_kind,
                        "text": _compact_identity_text(text),
                    },
                )
            )
    summary = str(card_payload.get("summary") or "").strip()
    if summary:
        candidates.append(
            (
                (int(normalized_surface in normalize_surface(summary)), 1, 0),
                {
                    "field_kind": "summary",
                    "text": _compact_identity_text(summary),
                },
            )
        )
    if not candidates:
        return {"field_kind": "unknown", "text": ""}
    return max(candidates, key=lambda item: item[0])[1]


def _compact_identity_text(text: str, *, limit: int = 280) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _identity_schema() -> dict[str, Any]:
    member_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "proposal_id": {"type": "string"},
            "alias_kind": {
                "type": "string",
                "enum": sorted(_ALIAS_KINDS),
            },
        },
        "required": ["proposal_id", "alias_kind"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "identity_groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "members": {
                            "type": "array",
                            "minItems": 2,
                            "items": member_schema,
                        },
                        "canonical_member_proposal_id": {"type": "string"},
                        "canonical_label": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "members",
                        "canonical_member_proposal_id",
                        "canonical_label",
                        "reason",
                    ],
                },
            },
            "aliases_to_existing": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "proposal_id": {"type": "string"},
                        "target_concept_id": {"type": "string"},
                        "alias_kind": {
                            "type": "string",
                            "enum": sorted(_ALIAS_KINDS),
                        },
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "proposal_id",
                        "target_concept_id",
                        "alias_kind",
                        "reason",
                    ],
                },
            },
            "canonicalizations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "proposal_id": {"type": "string"},
                        "canonical_label": {"type": "string"},
                        "alias_kind": {
                            "type": "string",
                            "enum": sorted(_CANONICALIZATION_ALIAS_KINDS),
                        },
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "proposal_id",
                        "canonical_label",
                        "alias_kind",
                        "reason",
                    ],
                },
            },
        },
        "required": [
            "identity_groups",
            "aliases_to_existing",
            "canonicalizations",
        ],
    }


def _hierarchy_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "primary_edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "child_concept_id": {"type": "string"},
                        "parent_concept_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "child_concept_id",
                        "parent_concept_id",
                        "reason",
                    ],
                },
            },
            "related_edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "left_concept_id": {"type": "string"},
                        "right_concept_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "left_concept_id",
                        "right_concept_id",
                        "reason",
                    ],
                },
            },
        },
        "required": ["primary_edges", "related_edges"],
    }
