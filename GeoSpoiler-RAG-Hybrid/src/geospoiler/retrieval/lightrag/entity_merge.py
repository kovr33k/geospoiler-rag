"""Safe LightRAG entity merge planning and application."""

import json
from pathlib import Path
from typing import Any

from lightrag import LightRAG

from geospoiler import config
from geospoiler.providers.runtime import logger
from geospoiler.retrieval.lightrag.extraction import _canonicalize_entity_name


def _entity_names_index_path() -> Path:
    """Path to the doc -> entity name index created by LightRAG."""
    return config.RAG_STORAGE_DIR / "kv_store_full_entities.json"


def _load_all_entity_names() -> list[str]:
    """Collect the unique entity labels currently present in the active graph."""
    path = _entity_names_index_path()
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    names: dict[str, None] = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        for name in entry.get("entity_names", []):
            if isinstance(name, str) and name.strip():
                names[name.strip()] = None
    return list(names.keys())


def _entity_name_preference_key(name: str) -> tuple[int, int, str]:
    """Prefer human-readable casing when auto-choosing a canonical label."""
    stripped = name.strip()
    is_all_caps = stripped.isupper() and any(ch.isalpha() for ch in stripped)
    starts_lower = bool(stripped) and stripped[0].islower()
    return (
        1 if is_all_caps and len(stripped) > 4 else 0,
        1 if starts_lower else 0,
        stripped.casefold(),
    )


def plan_safe_entity_merges(entity_names: list[str]) -> list[dict[str, Any]]:
    """
    Plan only the merges that are considered safe enough for automatic cleanup.

    Safe groups currently include:
    - exact case-only variants of the same entity label
    - explicit alias mappings declared in config and normalized by _canonicalize_entity_name()
    """
    merge_groups: dict[str, set[str]] = {}

    # Safe case-only duplicates, e.g. HAMAS -> Hamas, al-Qaeda -> Al-Qaeda
    casefold_groups: dict[str, list[str]] = {}
    for name in entity_names:
        casefold_groups.setdefault(name.casefold(), []).append(name)
    for variants in casefold_groups.values():
        deduped = sorted(set(variants))
        if len(deduped) < 2:
            continue
        target = sorted(deduped, key=_entity_name_preference_key)[0]
        sources = {name for name in deduped if name != target}
        if sources:
            merge_groups.setdefault(target, set()).update(sources)

    # Explicit alias-based merges from project config, e.g. USA -> United States
    for name in entity_names:
        canonical = _canonicalize_entity_name(name)
        if canonical == name:
            continue
        merge_groups.setdefault(canonical, set()).add(name)

    planned = []
    already_sources: set[str] = set()
    for target in sorted(merge_groups):
        sources = sorted(
            src
            for src in merge_groups[target]
            if src != target and src not in already_sources
        )
        if not sources:
            continue
        planned.append({"target": target, "sources": sources})
        already_sources.update(sources)
    return planned


async def auto_fix_safe_entity_merges(rag: LightRAG) -> list[dict[str, Any]]:
    """Apply safe entity merges directly to the active LightRAG graph."""
    plans = plan_safe_entity_merges(_load_all_entity_names())
    applied: list[dict[str, Any]] = []

    for plan in plans:
        try:
            await rag.amerge_entities(plan["sources"], plan["target"])
            applied.append(plan)
            logger.info(
                "Auto-fixed entity aliases: %s -> %s",
                ", ".join(plan["sources"]),
                plan["target"],
            )
        except Exception as exc:
            logger.warning(
                "Failed to auto-merge entities into %s (%s): %s",
                plan["target"],
                ", ".join(plan["sources"]),
                exc,
            )

    return applied
