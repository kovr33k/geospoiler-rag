"""
Retrieval Composer — orchestrates multi-index search across LightRAG and Enriched Cards.
Supports multiple retrieval modes optimized for different analytical tasks.
"""

import json
import logging
from dataclasses import dataclass, field

from lightrag import LightRAG

from geospoiler import config
from geospoiler.answering.query import query_rag_result
from geospoiler.retrieval.cards import shadow_search
from geospoiler.retrieval.cards.card_fts import (
    CardFtsMatch,
    YouTubeSegmentFtsMatch,
    search_card_index,
    search_youtube_segments,
)
from geospoiler.sources.registry import SourcePassport, resolve_source

logger = logging.getLogger("geospoiler.retrieval.composer")


@dataclass
class SearchResult:
    source_path: str
    card_path: str | None
    title: str
    url: str
    relevance_reason: str
    snippets: list[str] = field(default_factory=list)
    is_primary: bool = True
    source_id: str = ""
    primary_url: str = ""
    segment_hits: list["YouTubeSegmentHit"] = field(default_factory=list)


@dataclass
class YouTubeSegmentHit:
    segment_id: str
    segment_index: int
    start_seconds: float | None
    end_seconds: float | None
    url: str
    snippet: str
    score: float


@dataclass
class SearchPackage:
    query: str
    mode: str
    llm_answer: str
    primary_results: list[SearchResult]
    secondary_results: list[SearchResult]


@dataclass(frozen=True)
class CardSearchHit:
    source_path: str
    card_path: str | None
    title: str
    url: str
    score: float
    snippet: str
    backend: str
    source_id: str = ""


def _load_all_cards() -> list[dict]:
    cards = []
    enriched_dir = config.ENRICHED_DIR
    if not enriched_dir.exists():
        return cards

    for channel_dir in enriched_dir.iterdir():
        if not channel_dir.is_dir():
            continue
        for card_path in channel_dir.glob("*.enriched.json"):
            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
                card["_path"] = str(card_path)
                cards.append(card)
            except Exception:
                pass
    return cards


_CARDS_ONLY_MODES = {"shadow", "cards", "cards-only"}


async def search(rag: LightRAG | None, query: str, mode: str = "recall") -> SearchPackage:
    """
    Execute a multi-index search based on the specified mode.
    Modes:
      - recall: Broadest search (LightRAG mix + shadow keyword)
      - thesis: Focus on high-level analytical claims
      - entity: Focus on specific actors/locations
      - shadow/cards/cards-only: Fast enriched-card keyword search without LightRAG/LLM
    """
    mode = mode.strip().lower()
    logger.info(f"Composer executing search (mode={mode}) for: {query}")

    llm_answer = ""
    primary = {}
    secondary = {}
    cards_only = mode in _CARDS_ONLY_MODES

    # 1. Run LightRAG unless this is an explicit cards-only diagnostic search.
    lightrag_mode_map = {
        "recall": "mix" if config.RERANKER_ENABLED else "hybrid",
        "thesis": "global",
        "entity": "local"
    }
    lr_mode = lightrag_mode_map.get(mode, "hybrid")

    # We use "source" profile to get concrete citations if possible
    lr_profile = "source" if mode in ["recall", "entity"] else "overview"

    if cards_only:
        llm_answer = "Cards-only search: LightRAG/LLM query was not run."
    else:
        if rag is None:
            raise ValueError("rag is required unless mode is shadow/cards/cards-only")
        lr_result = await query_rag_result(rag, query, mode=lr_mode, query_profile=lr_profile)
        llm_answer = lr_result.get("response", "No answer from LightRAG.")

    cards = _load_all_cards()
    path_to_card = _index_cards_by_path(cards)

    # Process LightRAG references
    # references is usually a list of dicts from LightRAG if we enabled it,
    # but currently our lightrag_loader just returns the result dict which might have 'references' depending on version.
    # We will also use shadow search as a strong backup.

    # 2. FTS ranks first; shadow contributes unique recall for terms SQLite
    # may not have indexed yet.
    card_hits = _search_card_hits(query, top_k=20)
    youtube_hits = _search_youtube_segment_hits(query)
    wiki_hits = _search_wiki_hubs(query)

    # 3. Mode-specific filtering on cards
    query_lower = query.lower()

    if mode == "thesis":
        for card in cards:
            theses = card.get("theses", [])
            for t in theses:
                thesis_text = t.get("text", "") if isinstance(t, dict) else str(t)
                if query_lower in thesis_text.lower():
                    path = card.get("provenance", {}).get("normalized_path", card["_path"])
                    if path not in primary:
                        primary[path] = _card_to_result(card, "Thesis Match")
                    primary[path].snippets.append(thesis_text)

    elif mode == "entity":
        for card in cards:
            entities = card.get("entities", {})
            found = False
            for ents in entities.values():
                for e in ents:
                    entity_text = e.get("text", "") if isinstance(e, dict) else str(e)
                    if query_lower in entity_text.lower():
                        found = True
                        break
            if found:
                path = card.get("provenance", {}).get("normalized_path", card["_path"])
                if path not in primary:
                    primary[path] = _card_to_result(card, "Entity Match")

    for hit in wiki_hits:
        key = f"wiki:{hit.scope_key}"
        source_refs = (
            hit.source_refs.get("sources", [])
            if isinstance(hit.source_refs, dict)
            else []
        )
        first_source = source_refs[0] if source_refs else {}
        primary[key] = SearchResult(
            source_path=f"wiki://hub/{hit.scope_key}",
            card_path=None,
            title=hit.title,
            url=str(first_source.get("url") or ""),
            relevance_reason="Approved Wiki hub",
            snippets=[hit.snippet] if hit.snippet else [],
            source_id=str(first_source.get("source_id") or ""),
        )

    # 4. Integrate local card-search results
    for hit in card_hits:
        # If it's not already in primary, add it to secondary (or primary for broad/card-only modes).
        key = hit.source_path or hit.card_path or hit.title
        if key not in primary:
            card = _lookup_card_for_hit(path_to_card, hit)
            res = (
                _card_to_result(card, _card_search_reason(hit))
                if card
                else _hit_to_result(hit, _card_search_reason(hit))
            )
            if hit.snippet:
                res.snippets.append(hit.snippet)

            if (mode == "recall" and len(primary) < 5) or cards_only:
                primary[key] = res
            else:
                if key not in secondary:
                    secondary[key] = res

    # Segment hits are grouped below their episode card. They never become
    # independent top-level sources, even when several segments match.
    _attach_youtube_segment_hits(
        youtube_hits,
        cards=cards,
        path_to_card=path_to_card,
        primary=primary,
        secondary=secondary,
        cards_only=cards_only,
        mode=mode,
    )

    # Convert to lists
    primary_list = list(primary.values())
    secondary_list = list(secondary.values())

    return SearchPackage(
        query=query,
        mode=mode,
        llm_answer=llm_answer,
        primary_results=primary_list,
        secondary_results=secondary_list,
    )


def _card_to_result(card: dict, reason: str) -> SearchResult:
    prov = card.get("provenance", {})
    source_id = str(prov.get("source_id") or "").strip()
    passport = _resolve_source_passport(source_id)
    source_path = prov.get("normalized_path") or ""
    card_path = card.get("_path")
    title = str(prov.get("source_title") or "").strip() or f"{prov.get('channel') or '?'} - {(prov.get('date') or '?')[:10]}"
    url = prov.get("post_url", "")
    primary_url = ""
    if passport:
        source_id = passport.source_id
        source_path = passport.normalized_file or source_path
        card_path = passport.card_path or card_path
        if not str(prov.get("source_title") or "").strip():
            title = _title_from_passport(passport, title)
        url = passport.primary_url or passport.post_url or url
        primary_url = passport.primary_url

    return SearchResult(
        source_path=source_path,
        card_path=card_path,
        title=title,
        url=url,
        relevance_reason=reason,
        snippets=[],
        source_id=source_id,
        primary_url=primary_url,
    )


def _index_cards_by_path(cards: list[dict]) -> dict[str, dict]:
    indexed = {}
    for card in cards:
        provenance = card.get("provenance", {}) if isinstance(card.get("provenance"), dict) else {}
        for key in (provenance.get("normalized_path"), card.get("_path")):
            if key:
                indexed[str(key)] = card
                indexed[str(config.logical_project_path(key))] = card
    return indexed


def _index_cards_by_source_id(cards: list[dict]) -> dict[str, dict]:
    indexed = {}
    for card in cards:
        provenance = card.get("provenance", {}) if isinstance(card.get("provenance"), dict) else {}
        source_id = str(provenance.get("source_id") or "").strip()
        if source_id:
            indexed[source_id] = card
    return indexed


def _search_card_hits(query: str, top_k: int = 20) -> list[CardSearchHit]:
    try:
        fts_matches = search_card_index(query, top_k=top_k, db_path=config.CARD_FTS_DB_PATH)
    except Exception as exc:
        logger.warning("Card FTS search failed; continuing with shadow_search: %s", exc)
        fts_matches = []

    try:
        shadow_matches = shadow_search.search(query, top_k=top_k)
    except Exception as exc:
        logger.warning("Shadow card search failed; continuing with FTS results: %s", exc)
        shadow_matches = []

    ordered = [_fts_match_to_hit(match) for match in fts_matches]
    ordered.extend(_shadow_match_to_hit(match) for match in shadow_matches)

    merged: list[CardSearchHit] = []
    seen_keys: set[str] = set()
    for hit in ordered:
        keys = _card_hit_identity_keys(hit)
        if keys & seen_keys:
            continue
        merged.append(hit)
        seen_keys.update(keys)
        if len(merged) >= max(1, top_k):
            break
    return merged


def _search_youtube_segment_hits(query: str) -> list[YouTubeSegmentFtsMatch]:
    try:
        return search_youtube_segments(
            query,
            top_k=config.YOUTUBE_SEGMENT_SEARCH_TOP_K,
            db_path=config.CARD_FTS_DB_PATH,
        )
    except Exception as exc:
        logger.warning("YouTube segment search failed; continuing without segment hits: %s", exc)
        return []


def _search_wiki_hubs(query: str):
    if (
        not config.WIKI_ENABLED
        or not config.HYBRID_QUERY_WIKI_ENABLED
        or not config.WIKI_STATE_DB_PATH.exists()
    ):
        return []
    try:
        from geospoiler.wiki.schema import connect_database
        from geospoiler.wiki.search import search_wiki

        connection = connect_database(config.WIKI_STATE_DB_PATH)
        try:
            return list(
                search_wiki(
                    connection,
                    query,
                    limit=config.HYBRID_QUERY_WIKI_TOP_K,
                    document_kinds=("hub",),
                )
            )
        finally:
            connection.close()
    except Exception as exc:
        logger.warning("Wiki hub search failed; continuing without Wiki: %s", exc)
        return []


def _attach_youtube_segment_hits(
    hits: list[YouTubeSegmentFtsMatch],
    *,
    cards: list[dict],
    path_to_card: dict[str, dict],
    primary: dict[str, SearchResult],
    secondary: dict[str, SearchResult],
    cards_only: bool,
    mode: str,
) -> None:
    by_source = _index_cards_by_source_id(cards)
    per_episode: dict[str, int] = {}
    for hit in hits:
        card = by_source.get(hit.parent_source_id)
        if not card:
            logger.warning("Skipping orphan YouTube segment %s", hit.segment_id)
            continue
        if per_episode.get(hit.parent_source_id, 0) >= config.YOUTUBE_SEGMENT_HITS_PER_EPISODE:
            continue
        key = str(card.get("provenance", {}).get("normalized_path") or card.get("_path"))
        result = primary.get(key) or secondary.get(key)
        if result is None:
            result = _card_to_result(card, "YouTube segment match")
            if cards_only or (mode == "recall" and len(primary) < 5):
                primary[key] = result
            else:
                secondary[key] = result

        result.segment_hits.append(
            YouTubeSegmentHit(
                segment_id=hit.segment_id,
                segment_index=hit.segment_index,
                start_seconds=hit.start_seconds,
                end_seconds=hit.end_seconds,
                url=hit.start_url,
                snippet=hit.snippet,
                score=hit.score,
            )
        )
        per_episode[hit.parent_source_id] = per_episode.get(hit.parent_source_id, 0) + 1


def _card_hit_identity_keys(hit: CardSearchHit) -> set[str]:
    """Return stable identities that can connect the same hit across backends."""
    keys: set[str] = set()
    if hit.source_id:
        keys.add(f"source:{hit.source_id.strip().casefold()}")
    if hit.card_path:
        keys.add(f"card:{_normalize_hit_path(hit.card_path)}")
    if hit.source_path:
        keys.add(f"normalized:{_normalize_hit_path(hit.source_path)}")
    if not keys:
        keys.add(f"title:{hit.title.strip().casefold()}")
    return keys


def _normalize_hit_path(value: str) -> str:
    if config.DATA_LAYOUT_VERSION == 2:
        value = str(config.logical_project_path(value))
    return value.strip().replace("\\", "/").casefold()


def _fts_match_to_hit(match: CardFtsMatch) -> CardSearchHit:
    return CardSearchHit(
        source_path=match.normalized_file,
        card_path=match.card_path or None,
        title=match.title,
        url=match.post_url,
        score=match.score,
        snippet=match.snippet,
        backend="fts",
        source_id=match.source_id,
    )


def _shadow_match_to_hit(match: shadow_search.ShadowMatch) -> CardSearchHit:
    return CardSearchHit(
        source_path=match.source_path,
        card_path=match.card_path,
        title=match.title,
        url="",
        score=match.score,
        snippet=match.snippet,
        backend="shadow",
    )


def _lookup_card_for_hit(path_to_card: dict[str, dict], hit: CardSearchHit) -> dict | None:
    for key in (hit.source_path, hit.card_path or ""):
        if key and key in path_to_card:
            return path_to_card[key]
        if key and str(config.logical_project_path(key)) in path_to_card:
            return path_to_card[str(config.logical_project_path(key))]
    return None


def _hit_to_result(hit: CardSearchHit, reason: str) -> SearchResult:
    passport = _resolve_source_passport(hit.source_id)
    source_path = hit.source_path
    card_path = hit.card_path
    title = hit.title
    url = hit.url
    source_id = hit.source_id
    primary_url = ""
    if passport:
        source_path = passport.normalized_file or source_path
        card_path = passport.card_path or card_path
        title = _title_from_passport(passport, title)
        url = passport.primary_url or passport.post_url or url
        source_id = passport.source_id
        primary_url = passport.primary_url

    return SearchResult(
        source_path=source_path,
        card_path=card_path,
        title=title,
        url=url,
        relevance_reason=reason,
        snippets=[],
        source_id=source_id,
        primary_url=primary_url,
    )


def _card_search_reason(hit: CardSearchHit) -> str:
    if hit.backend == "fts":
        return f"FTS Match (BM25 score: {hit.score:.3g})"
    return f"Shadow Match (score: {hit.score:.1f})"


def _resolve_source_passport(source_id: str) -> SourcePassport | None:
    if not source_id:
        return None
    try:
        return resolve_source(source_id, db_path=config.SOURCE_REGISTRY_DB_PATH)
    except Exception as exc:
        logger.debug("Source registry lookup failed for %s: %s", source_id, exc)
        return None


def _title_from_passport(passport: SourcePassport, fallback: str) -> str:
    channel = passport.channel_name or "?"
    date = passport.date[:10] if passport.date else "?"
    title = f"{channel} - {date}"
    return title if title != "? - ?" else fallback
