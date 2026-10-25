"""Streamlit rendering for authoritative Wiki review decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping

import streamlit as st

from geospoiler import config
from geospoiler.wiki.hierarchy import (
    approve_hierarchy_proposal,
    defer_hierarchy_proposal,
    list_concept_tree,
    list_hierarchy_proposals,
    reject_hierarchy_proposal,
    review_hierarchy_batch,
)
from geospoiler.wiki.registry import (
    approve_proposal,
    defer_proposal,
    list_concepts,
    list_proposals,
    normalize_surface,
    pending_proposal_count,
    reject_proposal,
)
from geospoiler.wiki.relations import list_pending_ambiguities, resolve_ambiguity
from geospoiler.wiki.schema import connect_database
from geospoiler.wiki.service import refresh_wiki_after_review, wiki_status
from geospoiler.wiki.sidecars import get_manual_sidecar, save_manual_sidecar


def render_wiki_review() -> None:
    """Render all Wiki proposal classes and approved-concept maintenance."""
    try:
        connection = connect_database(config.WIKI_STATE_DB_PATH)
    except Exception as exc:
        st.error(f"Не удалось открыть Wiki state: {exc}")
        return
    try:
        status = wiki_status(connection)
        st.markdown(
            """
            <div class="main-header">
                <h1>GeoSpoiler Wiki</h1>
                <p>Одобрение устойчивых concepts, aliases, неоднозначностей и структуры hubs.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        _render_status(status)
        if st.button(
            "Пересчитать связи, предложения и hubs",
            type="primary",
            use_container_width=True,
        ):
            with st.spinner("Обновляю derived Wiki и одноразовые Markdown hubs…"):
                try:
                    refresh_wiki_after_review(
                        connection,
                        output_directory=config.WIKI_OUTPUT_DIR,
                        sidecar_directory=config.WIKI_SIDECAR_DIR,
                    )
                except Exception as exc:
                    st.error(f"Wiki refresh не завершён: {exc}")
                else:
                    st.success("Wiki обновлена.")
                    st.rerun()

        registry_tab, ambiguity_tab, hierarchy_tab, concepts_tab = st.tabs(
            [
                f"Concepts и aliases ({status['pending_proposals']})",
                f"Неоднозначности ({status['pending_ambiguities']})",
                f"Иерархия ({status['pending_hierarchy']})",
                f"Approved hubs ({status['approved_concepts']})",
            ]
        )
        with registry_tab:
            _render_registry_proposals(connection)
        with ambiguity_tab:
            _render_ambiguities(connection)
        with hierarchy_tab:
            _render_hierarchy(connection)
        with concepts_tab:
            _render_approved_concepts(connection)
    finally:
        connection.close()


def _render_status(status: dict[str, int]) -> None:
    columns = st.columns(4)
    values = (
        ("Approved concepts", status["approved_concepts"]),
        ("Concept proposals", status["pending_proposals"]),
        ("Hierarchy proposals", status["pending_hierarchy"]),
        ("Ambiguities", status["pending_ambiguities"]),
    )
    for column, (label, value) in zip(columns, values, strict=True):
        column.metric(label, value)


def _render_registry_proposals(connection) -> None:
    proposals = list_proposals(connection)
    if not proposals:
        st.success("Новых concepts или aliases для решения нет.")
        return
    actionable = _actionable_registry_proposals(proposals)
    if len(actionable) != len(proposals):
        st.caption(
            f"{len(proposals)} предложений сгруппированы в "
            f"{len(actionable)} пользовательских решений. "
            "Исходные candidates внутри merge/alias не нужно подтверждать отдельно."
        )
    concepts = list_concepts(connection)
    concept_labels = {
        concept.concept_id: f"{concept.canonical_label} · {concept.source_category}"
        for concept in concepts
    }
    for proposal in actionable:
        key = _widget_key(proposal.proposal_id)
        with st.expander(_proposal_title(proposal), expanded=False):
            st.info(_proposal_action_text(proposal))
            st.markdown("**Почему система показала это предложение**")
            st.write(_proposal_reason_text(proposal))

            examples = _proposal_evidence_examples(connection, proposal)
            st.markdown("**Что об этом говорится в Enriched-карточках**")
            if not examples:
                st.warning(
                    "Для предложения не удалось восстановить читаемый фрагмент. "
                    "Одобрять его без проверки исходной карточки не стоит."
                )
            for ordinal, example in enumerate(examples, 1):
                st.markdown(f"**{ordinal}. {example['source_label']}**")
                source_meta = " · ".join(
                    value
                    for value in (
                        example["source_kind_label"],
                        example["date"],
                    )
                    if value
                )
                if source_meta:
                    st.caption(source_meta)
                st.write(example["snippet"])
                evidence_meta = " · ".join(
                    value
                    for value in (
                        example["field_label"],
                        (
                            f"форма: {example['matched_surface']}"
                            if example["matched_surface"]
                            else ""
                        ),
                        (
                            f"роль: {example['source_role']}"
                            if example["source_role"]
                            else ""
                        ),
                        (
                            f"значимость: {example['salience']}"
                            if example["salience"]
                            else ""
                        ),
                    )
                    if value
                )
                if evidence_meta:
                    st.caption(evidence_meta)
                if example["url"]:
                    st.markdown(f"[Открыть источник]({example['url']})")

            st.markdown("**Что будет сохранено после одобрения**")
            canonical_label = st.text_input(
                "Название hub в нормальной форме",
                value=proposal.display_label,
                key=f"label_{key}",
                help=(
                    "Проверь именительный падеж: например, «Европы» лучше "
                    "исправить на «Европа». Это станет названием approved hub."
                ),
            )
            concept_kind = (
                "topic" if proposal.proposal_kind == "topic" else "entity"
            )
            source_category = proposal.source_category or "other"
            description = ""
            rationale = ""
            show_advanced = st.toggle(
                "Дополнительные настройки и технические детали",
                key=f"advanced_{key}",
            )
            if show_advanced:
                concept_kind = st.selectbox(
                    "Тип concept",
                    ("entity", "topic"),
                    index=1 if concept_kind == "topic" else 0,
                    key=f"kind_{key}",
                )
                source_category = st.text_input(
                    "Категория concept",
                    value=source_category,
                    key=f"category_{key}",
                )
                description = st.text_area(
                    "Короткое описание (необязательно)",
                    key=f"description_{key}",
                    height=80,
                )
                rationale = st.text_input(
                    "Комментарий к решению (необязательно)",
                    key=f"rationale_{key}",
                )
                st.caption(
                    f"{_proposal_support_label(proposal)} · "
                    f"evidence records: {proposal.evidence_count} · "
                    f"candidate key: {proposal.normalized_candidate_key}"
                )
                st.json(proposal.payload, expanded=False)
            target_options = (
                list(concept_labels)
                if proposal.proposal_kind == "alias"
                else ["Создать новый concept", *concept_labels]
            )
            default_target = str(proposal.payload.get("target_concept_id") or "")
            default_index = (
                target_options.index(default_target)
                if default_target in target_options
                else 0
            )
            if proposal.proposal_kind in {"alias", "merge", "split"}:
                selected_target = st.selectbox(
                    "Привязка",
                    target_options,
                    index=default_index,
                    format_func=lambda value, options=target_options: (
                        value
                        if value == options[0] and value not in concept_labels
                        else concept_labels[value]
                    ),
                    key=f"target_{key}",
                )
            else:
                selected_target = target_options[0]
            approve_col, reject_col, defer_col = st.columns(3)
            if approve_col.button(
                "Одобрить",
                key=f"approve_registry_{key}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    approve_proposal(
                        connection,
                        proposal.proposal_id,
                        canonical_label=canonical_label,
                        concept_kind=concept_kind,
                        source_category=source_category,
                        description=description,
                        target_concept_id=(
                            None if selected_target not in concept_labels else selected_target
                        ),
                        rationale=rationale,
                    )
                    _refresh_if_registry_batch_finished(connection)
                except Exception as exc:
                    st.error(f"Не удалось одобрить: {exc}")
                else:
                    st.rerun()
            if reject_col.button(
                "Отклонить",
                key=f"reject_registry_{key}",
                use_container_width=True,
            ):
                try:
                    reject_proposal(
                        connection,
                        proposal.proposal_id,
                        rationale=rationale or "Отклонено в Wiki reviewer",
                    )
                    _refresh_if_registry_batch_finished(connection)
                except Exception as exc:
                    st.error(f"Не удалось отклонить: {exc}")
                else:
                    st.rerun()
            if defer_col.button(
                "Отложить",
                key=f"defer_registry_{key}",
                use_container_width=True,
            ):
                try:
                    defer_proposal(
                        connection,
                        proposal.proposal_id,
                        rationale=rationale,
                    )
                    _refresh_if_registry_batch_finished(connection)
                except Exception as exc:
                    st.error(f"Не удалось отложить: {exc}")
                else:
                    st.rerun()


def _proposal_support_label(proposal) -> str:
    if proposal.proposal_kind == "merge":
        if proposal.payload.get("identity_review_kind") == "canonicalization":
            return "Luna canonical-form proposal · manual approval required"
        return "Luna identity group · manual approval required"
    threshold = proposal.payload.get("threshold") or {}
    qualification = proposal.payload.get("qualification") or {}
    if qualification.get("rule_id") == "primary_youtube_segment_topic":
        return "primary-тема содержательного YouTube-сегмента"
    source_families = int(
        threshold.get("observed_distinct_source_families")
        or proposal.cluster_count
    )
    return (
        f"{proposal.cluster_count} content clusters · "
        f"{source_families} source families"
    )


_CATEGORY_LABELS = {
    "people": "человек",
    "organizations": "организация",
    "countries": "страна",
    "locations": "место или регион",
    "military_units": "воинское подразделение",
    "equipment": "техника или оборудование",
    "weapons": "оружие",
    "programs_projects": "программа или проект",
    "case_topic": "конкретный сюжет",
    "military_topic": "военная тема",
    "technology_topic": "технологическая тема",
    "political_topic": "политическая тема",
    "economic_topic": "экономическая тема",
    "other": "другая категория",
}

_SOURCE_KIND_LABELS = {
    "telegram": "Telegram/исходная карточка",
    "youtube": "YouTube-видео",
    "youtube_segment": "YouTube-сегмент",
}

_SNIPPET_FIELDS = (
    ("events", "description", "Событие", 5),
    ("key_points", "text", "Claim / ключевой пункт", 4),
    ("theses", "text", "Тезис", 3),
    ("quotes", "text", "Цитата", 2),
)


def _proposal_title(proposal) -> str:
    category = _CATEGORY_LABELS.get(
        proposal.source_category,
        proposal.source_category or "другая категория",
    )
    if proposal.proposal_kind == "topic":
        return f"Новый тематический hub «{proposal.display_label}» · {category}"
    if proposal.proposal_kind == "entity":
        return f"Новый hub-сущность «{proposal.display_label}» · {category}"
    if proposal.proposal_kind == "alias":
        target_label = str(
            proposal.payload.get("target_concept_label") or "существующий hub"
        )
        return f"Alias «{proposal.display_label}» → hub «{target_label}»"
    if proposal.proposal_kind == "merge":
        labels = _identity_member_labels(proposal)
        if proposal.payload.get("identity_review_kind") == "canonicalization":
            source_label = labels[0] if labels else "исходная форма"
            return (
                f"Нормальная форма: «{source_label}» → "
                f"«{proposal.display_label}»"
            )
        forms = " / ".join(labels)
        suffix = f": {forms}" if forms else ""
        return f"Один hub «{proposal.display_label}»{suffix}"
    if proposal.proposal_kind == "split":
        return f"Разделить неоднозначное значение «{proposal.display_label}»"
    return f"Проверить Wiki-предложение «{proposal.display_label}»"


def _proposal_action_text(proposal) -> str:
    if proposal.proposal_kind == "topic":
        return (
            "Предлагается создать approved тематический Wiki hub "
            f"«{proposal.display_label}». Связанные claims и события будут "
            "показываться внутри него со ссылками на исходные карточки."
        )
    if proposal.proposal_kind == "entity":
        return (
            f"Предлагается создать approved Wiki hub «{proposal.display_label}». "
            "Это одобрение сущности, а не подтверждение истинности связанных claims."
        )
    if proposal.proposal_kind == "alias":
        target_label = str(
            proposal.payload.get("target_concept_label") or "существующий hub"
        )
        return (
            f"Luna предлагает считать «{proposal.display_label}» ещё одним "
            f"названием hub «{target_label}». После одобрения новый hub не "
            "создаётся: форма станет alias существующего."
        )
    if proposal.proposal_kind == "merge":
        labels = _identity_member_labels(proposal)
        if proposal.payload.get("identity_review_kind") == "canonicalization":
            source_label = labels[0] if labels else "исходная форма"
            return (
                f"Luna предлагает назвать hub «{proposal.display_label}», потому "
                f"что «{source_label}» — форма того же названия не в словарном "
                "виде. После одобрения исходная форма сохранится как alias. До "
                "одобрения registry не изменяется."
            )
        forms = (
            ", ".join(f"«{label}»" for label in labels)
            if labels
            else "перечисленные кандидаты"
        )
        return (
            f"Luna предлагает считать формы {forms} одним concept. После "
            f"одобрения появится один hub «{proposal.display_label}», а все "
            "исходные формы сохранятся как aliases. До одобрения registry не "
            "изменяется."
        )
    if proposal.proposal_kind == "split":
        return (
            "Одна текстовая форма, вероятно, обозначает разные concepts. "
            "Предлагается явно разделить их."
        )
    return "Предлагается authoritative изменение Wiki registry."


def _proposal_reason_text(proposal) -> str:
    if proposal.proposal_kind in {"alias", "merge"}:
        rationale = str(proposal.payload.get("rationale") or "").strip()
        if rationale:
            return rationale
        return (
            "Resolver нашёл строгое совпадение identity, но не сохранил "
            "читаемое объяснение. Проверь примеры ниже перед одобрением."
        )
    threshold = proposal.payload.get("threshold") or {}
    qualification = proposal.payload.get("qualification") or {}
    if qualification.get("rule_id") == "primary_youtube_segment_topic":
        return (
            f"«{proposal.display_label}» размечена как primary-тема "
            "содержательного YouTube-сегмента. Поэтому система показала её "
            "после одной source family; hub всё равно появится только после "
            "твоего одобрения."
        )
    clusters = int(
        threshold.get("observed_distinct_content_clusters")
        or proposal.cluster_count
    )
    families = int(
        threshold.get("observed_distinct_source_families")
        or proposal.cluster_count
    )
    minimum_clusters = int(
        threshold.get("minimum_distinct_content_clusters") or 2
    )
    minimum_families = int(
        threshold.get("minimum_distinct_source_families") or 2
    )
    return (
        f"Форма «{proposal.display_label}» встретилась в {clusters} разных "
        f"содержательных кластерах из {families} независимых source families. "
        f"Порог показа: минимум {minimum_clusters} кластера из "
        f"{minimum_families} families."
    )


def _best_proposal_snippet(
    card_payload: Mapping[str, object],
    normalized_surface: str,
) -> dict[str, str]:
    candidates: list[tuple[tuple[int, int, int], dict[str, str]]] = []
    normalized_candidate = normalize_surface(normalized_surface)
    for field, text_key, field_label, priority in _SNIPPET_FIELDS:
        values = card_payload.get(field) or []
        if not isinstance(values, list):
            continue
        for ordinal, item in enumerate(values):
            if not isinstance(item, Mapping):
                continue
            text = str(item.get(text_key) or "").strip()
            if not text:
                continue
            matches = int(
                normalized_candidate in normalize_surface(text)
            )
            candidates.append(
                (
                    (matches, priority, -ordinal),
                    {
                        "field": field,
                        "field_label": field_label,
                        "text": _truncate_text(text),
                    },
                )
            )
    summary = str(card_payload.get("summary") or "").strip()
    if summary:
        matches = int(normalized_candidate in normalize_surface(summary))
        candidates.append(
            (
                (matches, 1, 0),
                {
                    "field": "summary",
                    "field_label": "Краткое содержание",
                    "text": _truncate_text(summary),
                },
            )
        )
    if not candidates:
        return {
            "field": "unknown",
            "field_label": "Фрагмент не найден",
            "text": "В карточке нет читаемого claim/event-фрагмента.",
        }
    return max(candidates, key=lambda item: item[0])[1]


def _proposal_evidence_examples(
    connection,
    proposal,
    *,
    limit: int = 4,
) -> tuple[dict[str, str], ...]:
    evidence_proposal_ids = _proposal_evidence_proposal_ids(proposal)
    placeholders = ", ".join("?" for _ in evidence_proposal_ids)
    rows = connection.execute(
        f"""
        SELECT
            evidence.evidence_payload_json,
            source.source_kind,
            source.external_key,
            revision.canonical_payload_json
        FROM concept_proposal_evidence AS evidence
        JOIN source_lineages AS source
          ON source.source_lineage_id = evidence.source_lineage_id
        JOIN card_revisions AS revision
          ON revision.card_revision_id = evidence.card_revision_id
         AND revision.source_lineage_id = evidence.source_lineage_id
        WHERE evidence.concept_proposal_id IN ({placeholders})
        ORDER BY
            source.external_key,
            evidence.card_revision_id,
            evidence.concept_proposal_evidence_id
        """,
        evidence_proposal_ids,
    ).fetchall()
    by_family: dict[
        tuple[str, str],
        tuple[tuple[int, int], dict[str, str]],
    ] = {}
    for row in rows:
        evidence = json.loads(row["evidence_payload_json"])
        card_payload = json.loads(row["canonical_payload_json"])
        normalized_surface = str(
            evidence.get("normalized_surface")
            or proposal.payload.get("normalized_surface")
            or normalize_surface(proposal.display_label)
        )
        snippet = _best_proposal_snippet(card_payload, normalized_surface)
        source = card_payload.get("source") or {}
        if not isinstance(source, Mapping):
            source = {}
        source_kind = str(row["source_kind"])
        source_label = str(
            source.get("source_title")
            or source.get("title")
            or source.get("channel")
            or row["external_key"]
        )
        source_url = str(
            source.get("start_url")
            or source.get("post_url")
            or ""
        )
        date = str(source.get("date") or "")
        if date:
            date = date[:10]
        family_id = str(
            evidence.get("source_family_id")
            or row["external_key"]
        )
        display_surface = str(
            evidence.get("display_surface")
            or normalized_surface
        )
        exact_match = int(
            normalize_surface(normalized_surface)
            in normalize_surface(snippet["text"])
        )
        rank = (
            exact_match,
            int(source_kind == "youtube_segment"),
        )
        example = {
            "source_label": source_label,
            "source_kind_label": _SOURCE_KIND_LABELS.get(
                source_kind,
                source_kind,
            ),
            "date": date,
            "url": source_url,
            "snippet": snippet["text"],
            "field_label": snippet["field_label"],
            "source_role": str(evidence.get("source_role") or ""),
            "salience": str(evidence.get("salience") or ""),
            "matched_surface": display_surface,
        }
        evidence_key = (normalize_surface(display_surface), family_id)
        current = by_family.get(evidence_key)
        if current is None or rank > current[0]:
            by_family[evidence_key] = (rank, example)
    examples = sorted(
        (value[1] for value in by_family.values()),
        key=lambda value: (
            value["source_label"].casefold(),
            value["source_label"],
            value["snippet"],
        ),
    )
    return tuple(examples[:limit])


def _identity_member_labels(proposal) -> tuple[str, ...]:
    values = proposal.payload.get("member_surfaces") or []
    labels: list[str] = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, Mapping):
                continue
            label = str(value.get("display_label") or "").strip()
            if label and label not in labels:
                labels.append(label)
    return tuple(labels)


def _proposal_evidence_proposal_ids(proposal) -> tuple[str, ...]:
    proposal_ids = [proposal.proposal_id]
    if proposal.proposal_kind == "merge":
        proposal_ids.extend(
            str(value)
            for value in proposal.payload.get("member_proposal_ids", [])
            if value
        )
    if proposal.proposal_kind == "alias":
        source_proposal_id = str(
            proposal.payload.get("source_proposal_id") or ""
        )
        if source_proposal_id:
            proposal_ids.append(source_proposal_id)
    return tuple(dict.fromkeys(proposal_ids))


def _truncate_text(text: str, *, limit: int = 520) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _render_ambiguities(connection) -> None:
    ambiguities = list_pending_ambiguities(connection)
    if not ambiguities:
        st.success("Неоднозначных claim-specific решений нет.")
        return
    for item in ambiguities:
        key = _widget_key(
            f"{item['occurrence_version_id']}:{item['normalized_surface']}"
        )
        payload = item["occurrence_payload"]
        claim_text = (
            payload.get("text")
            if isinstance(payload, dict)
            else json.dumps(payload, ensure_ascii=False)
        )
        with st.expander(
            f"{item['normalized_surface']} — {claim_text}",
            expanded=True,
        ):
            st.caption(item["reason"])
            candidates = item["candidates"]
            candidate_ids = [candidate["concept_id"] for candidate in candidates]
            candidate_by_id = {
                candidate["concept_id"]: candidate for candidate in candidates
            }
            selected = st.selectbox(
                "Наиболее точный референт для этого claim",
                candidate_ids,
                format_func=lambda concept_id, choices=candidate_by_id: (
                    f"{choices[concept_id]['canonical_label']} · "
                    f"{choices[concept_id]['source_category']}"
                ),
                key=f"ambiguity_candidate_{key}",
            )
            role = st.selectbox(
                "Роль",
                ("subject", "actor", "object", "context", "mentioned"),
                index=3,
                key=f"ambiguity_role_{key}",
            )
            rationale = st.text_input(
                "Почему",
                value=item["reason"],
                key=f"ambiguity_reason_{key}",
            )
            pin_col, unresolved_col = st.columns(2)
            if pin_col.button(
                "Закрепить выбор",
                key=f"ambiguity_pin_{key}",
                type="primary",
                use_container_width=True,
            ):
                try:
                    resolve_ambiguity(
                        connection,
                        occurrence_version_id=item["occurrence_version_id"],
                        normalized_surface=item["normalized_surface"],
                        selected_concept_id=selected,
                        relation_role=role,
                        rationale=rationale,
                    )
                    _refresh_derived(connection, use_luna=False)
                except Exception as exc:
                    st.error(f"Не удалось закрепить: {exc}")
                else:
                    st.rerun()
            if unresolved_col.button(
                "Оставить неоднозначным",
                key=f"ambiguity_unresolved_{key}",
                use_container_width=True,
            ):
                try:
                    resolve_ambiguity(
                        connection,
                        occurrence_version_id=item["occurrence_version_id"],
                        normalized_surface=item["normalized_surface"],
                        selected_concept_id=None,
                        rationale=rationale or "Оставлено неоднозначным пользователем",
                    )
                    _refresh_derived(connection, use_luna=False)
                except Exception as exc:
                    st.error(f"Не удалось сохранить решение: {exc}")
                else:
                    st.rerun()


def _render_hierarchy(connection) -> None:
    proposals = list_hierarchy_proposals(connection)
    if not proposals:
        st.success("Предложений по иерархии нет.")
        _render_tree(connection)
        return
    concepts = {concept.concept_id: concept for concept in list_concepts(connection)}
    batches: dict[str, list] = {}
    for proposal in proposals:
        batch_id = str(proposal.payload.get("batch_id") or "без batch")
        batches.setdefault(batch_id, []).append(proposal)
    for batch_id, batch in batches.items():
        st.subheader(f"Пакет: {batch_id}")
        batch_approve, batch_reject = st.columns(2)
        if batch_approve.button(
            f"Одобрить пакет ({len(batch)})",
            key=f"approve_batch_{_widget_key(batch_id)}",
            type="primary",
            use_container_width=True,
        ):
            errors = _apply_hierarchy_batch(
                connection,
                batch,
                decision="approve",
            )
            if errors:
                st.error("\n".join(errors))
            else:
                _refresh_derived(connection, use_luna=False)
                st.rerun()
        if batch_reject.button(
            f"Отклонить пакет ({len(batch)})",
            key=f"reject_batch_{_widget_key(batch_id)}",
            use_container_width=True,
        ):
            errors = _apply_hierarchy_batch(
                connection,
                batch,
                decision="reject",
            )
            if errors:
                st.error("\n".join(errors))
            else:
                st.rerun()
        for proposal in batch:
            child = concepts[proposal.child_concept_id].canonical_label
            other = concepts[proposal.other_concept_id].canonical_label
            relation = (
                f"{child} → {other}"
                if proposal.edge_kind == "primary_parent"
                else f"{child} ↔ {other}"
            )
            key = _widget_key(proposal.proposal_id)
            with st.expander(
                f"{relation} · {proposal.edge_kind}",
                expanded=False,
            ):
                st.write(proposal.rationale)
                rationale = st.text_input(
                    "Комментарий",
                    key=f"hierarchy_reason_{key}",
                )
                approve_col, reject_col, defer_col = st.columns(3)
                if approve_col.button(
                    "Одобрить",
                    key=f"hierarchy_approve_{key}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        approve_hierarchy_proposal(
                            connection,
                            proposal.proposal_id,
                            rationale=rationale,
                        )
                        _refresh_derived(connection, use_luna=False)
                    except Exception as exc:
                        st.error(f"Не удалось применить: {exc}")
                    else:
                        st.rerun()
                if reject_col.button(
                    "Отклонить",
                    key=f"hierarchy_reject_{key}",
                    use_container_width=True,
                ):
                    try:
                        reject_hierarchy_proposal(
                            connection,
                            proposal.proposal_id,
                            rationale=rationale or "Отклонено в Wiki reviewer",
                        )
                    except Exception as exc:
                        st.error(f"Не удалось отклонить: {exc}")
                    else:
                        st.rerun()
                if defer_col.button(
                    "Отложить",
                    key=f"hierarchy_defer_{key}",
                    use_container_width=True,
                ):
                    try:
                        defer_hierarchy_proposal(
                            connection,
                            proposal.proposal_id,
                            rationale=rationale,
                        )
                    except Exception as exc:
                        st.error(f"Не удалось отложить: {exc}")
                    else:
                        st.rerun()
    _render_tree(connection)


def _render_tree(connection) -> None:
    tree = list_concept_tree(connection)
    if not tree:
        return
    labels = {node.concept_id: node.canonical_label for node in tree}
    lines = []
    for node in tree:
        parent = (
            labels.get(node.parent_concept_id, "—")
            if node.parent_concept_id
            else "корень"
        )
        lines.append(
            f"- **{node.canonical_label}** ← {parent}; "
            f"подтем: {len(node.child_concept_ids)}, "
            f"related: {len(node.related_concept_ids)}"
        )
    st.markdown("### Текущее approved-дерево")
    st.markdown("\n".join(lines))


def _render_approved_concepts(connection) -> None:
    concepts = list_concepts(connection)
    if not concepts:
        st.info("Approved concepts пока нет.")
        return
    selected_id = st.selectbox(
        "Hub",
        [concept.concept_id for concept in concepts],
        format_func=lambda concept_id: next(
            concept.canonical_label
            for concept in concepts
            if concept.concept_id == concept_id
        ),
    )
    selected = next(
        concept for concept in concepts if concept.concept_id == selected_id
    )
    st.caption(
        f"{selected.concept_kind} · {selected.source_category} · "
        f"aliases: {', '.join(selected.aliases)}"
    )
    sidecar = get_manual_sidecar(connection, selected_id)
    markdown = st.text_area(
        "Ручной sidecar Markdown",
        value=sidecar.markdown_text,
        height=260,
        key=f"sidecar_{_widget_key(selected_id)}_{sidecar.generation}",
        help=(
            "Это authoritative текст. Generated hub можно удалить и "
            "пересобрать; эта заметка останется."
        ),
    )
    if st.button(
        "Сохранить sidecar и пересобрать hub",
        type="primary",
        use_container_width=True,
    ):
        try:
            save_manual_sidecar(
                connection,
                concept_id=selected_id,
                markdown_text=markdown,
                directory=config.WIKI_SIDECAR_DIR,
            )
            _refresh_derived(connection, use_luna=False)
        except Exception as exc:
            st.error(f"Не удалось сохранить sidecar: {exc}")
        else:
            st.success("Sidecar сохранён.")
            st.rerun()


def _refresh_if_registry_batch_finished(connection) -> None:
    if pending_proposal_count(connection) == 0:
        _refresh_derived(connection, use_luna=None)


def _refresh_derived(connection, *, use_luna: bool | None) -> None:
    refresh_wiki_after_review(
        connection,
        output_directory=config.WIKI_OUTPUT_DIR,
        sidecar_directory=config.WIKI_SIDECAR_DIR,
        use_luna=use_luna,
    )


def _apply_hierarchy_batch(
    connection,
    proposals,
    *,
    decision: str,
) -> list[str]:
    try:
        review_hierarchy_batch(
            connection,
            [proposal.proposal_id for proposal in proposals],
            decision=decision,
            rationale=(
                "Одобрен пакет в Wiki reviewer"
                if decision == "approve"
                else "Отклонён пакет в Wiki reviewer"
            ),
        )
    except Exception as exc:
        return [str(exc)]
    return []


def _widget_key(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)[-96:]


def _actionable_registry_proposals(proposals):
    """Hide source candidates already represented by a pending composite decision."""
    proposal_ids = {proposal.proposal_id for proposal in proposals}
    covered: set[str] = set()
    for proposal in proposals:
        if proposal.proposal_kind == "merge":
            covered.update(
                str(value)
                for value in proposal.payload.get("member_proposal_ids", [])
                if str(value) in proposal_ids
            )
        elif proposal.proposal_kind == "alias":
            source_id = str(
                proposal.payload.get("source_proposal_id") or ""
            )
            if source_id in proposal_ids:
                covered.add(source_id)
    priority = {
        "merge": 0,
        "alias": 1,
        "split": 2,
        "entity": 3,
        "topic": 3,
    }
    actionable = [
        proposal
        for proposal in proposals
        if proposal.proposal_id not in covered
    ]
    return tuple(
        sorted(
            actionable,
            key=lambda proposal: (
                priority.get(proposal.proposal_kind, 9),
                proposal.display_label.casefold(),
                proposal.proposal_id,
            ),
        )
    )
