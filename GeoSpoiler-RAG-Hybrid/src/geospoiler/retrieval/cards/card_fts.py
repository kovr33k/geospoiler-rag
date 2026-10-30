import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from geospoiler import config
from geospoiler.contracts.models import YouTubeSegmentCardV2
from geospoiler.retrieval.cards.card_text import card_search_text, strip_metadata_lines
from geospoiler.retrieval.cards.query_terms import add_compound_terms, expand_query_terms, matches_term

logger = logging.getLogger("geospoiler.retrieval.card_fts")

_TOKEN_RE = re.compile(r"\w{3,}", re.UNICODE)
_COVERAGE_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


@dataclass(frozen=True)
class CardFtsRecord:
    source_id: str
    card_path: str
    normalized_file: str
    post_url: str
    title: str
    search_text: str
    entities: str
    topics: str
    claim_types: str


@dataclass(frozen=True)
class CardFtsBuildStats:
    db_path: Path
    cards_seen: int
    cards_indexed: int
    cards_skipped: int


@dataclass(frozen=True)
class CardFtsMatch:
    source_id: str
    card_path: str
    normalized_file: str
    post_url: str
    title: str
    score: float
    snippet: str


@dataclass(frozen=True)
class YouTubeSegmentFtsBuildStats:
    db_path: Path
    segments_seen: int
    segments_indexed: int
    segments_skipped: int


@dataclass(frozen=True)
class YouTubeSegmentFtsMatch:
    segment_id: str
    parent_source_id: str
    video_id: str
    segment_index: int
    start_seconds: float | None
    end_seconds: float | None
    start_url: str
    card_path: str
    title: str
    score: float
    snippet: str


def rebuild_card_index(
    enriched_dir: Path = config.ENRICHED_DIR,
    db_path: Path = config.CARD_FTS_DB_PATH,
) -> CardFtsBuildStats:
    """Rebuild the local SQLite FTS5 index for enriched cards."""
    records = list(iter_card_records(enriched_dir))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(db_path)) as conn:
        _create_schema(conn)
        conn.execute("DELETE FROM cards_fts")
        conn.executemany(
            """
            INSERT INTO cards_fts (
                source_id,
                card_path,
                normalized_file,
                post_url,
                title,
                search_text,
                entities,
                topics,
                claim_types
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.source_id,
                    record.card_path,
                    record.normalized_file,
                    record.post_url,
                    record.title,
                    record.search_text,
                    record.entities,
                    record.topics,
                    record.claim_types,
                )
                for record in records
            ],
        )
        conn.commit()

    seen = _count_enriched_cards(enriched_dir)
    indexed = len(records)
    return CardFtsBuildStats(
        db_path=db_path,
        cards_seen=seen,
        cards_indexed=indexed,
        cards_skipped=max(0, seen - indexed),
    )


def search_card_index(
    query: str,
    top_k: int | None = 10,
    db_path: Path = config.CARD_FTS_DB_PATH,
) -> list[CardFtsMatch]:
    """Search card FTS; top_k=None returns every matching indexed card."""
    query_terms = _query_terms(query)
    match_query = _to_fts_query(query)
    if not match_query or not db_path.exists():
        return []

    limit = None if top_k is None else max(1, top_k)
    with closing(_connect_read_only(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT
                source_id,
                card_path,
                normalized_file,
                post_url,
                title,
                search_text,
                bm25(cards_fts) AS rank,
                snippet(cards_fts, 5, '...', '...', ' ', 24) AS snippet
            FROM cards_fts
            WHERE cards_fts MATCH ?
            ORDER BY rank, source_id, card_path, normalized_file
        """
        params: tuple[Any, ...] = (match_query,)
        if limit is not None:
            fetch_limit = max(limit, min(100, limit * 5))
            sql += " LIMIT ?"
            params += (fetch_limit,)
        rows = conn.execute(sql, params).fetchall()
        ranked = [
            (
                _coverage_score(str(row["search_text"] or ""), query_terms),
                float(row["rank"]),
                CardFtsMatch(
                    source_id=row["source_id"],
                    card_path=row["card_path"],
                    normalized_file=row["normalized_file"],
                    post_url=row["post_url"],
                    title=row["title"],
                    score=round(-float(row["rank"]), 6),
                    snippet=_clean_snippet(row["snippet"]),
                ),
            )
            for row in rows
        ]
        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1],
                item[2].source_id,
                item[2].card_path,
                item[2].normalized_file,
            )
        )
        matches = [item[2] for item in ranked]
        return matches if limit is None else matches[:limit]


def rebuild_youtube_segment_index(
    segments_dir: Path = config.YOUTUBE_SEGMENTS_DIR,
    db_path: Path = config.CARD_FTS_DB_PATH,
) -> YouTubeSegmentFtsBuildStats:
    """Rebuild the separate FTS index for YouTube child segments."""
    indexed = 0

    def counted_records():
        nonlocal indexed
        for record in iter_youtube_segment_records(segments_dir):
            indexed += 1
            yield record

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        _create_youtube_segment_schema(conn)
        conn.execute("DELETE FROM youtube_segments_fts")
        conn.executemany(
            """
            INSERT INTO youtube_segments_fts (
                segment_id, parent_source_id, video_id, segment_index,
                start_seconds, end_seconds, start_url, card_path, title, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            counted_records(),
        )
        conn.commit()
    seen = sum(1 for _ in segments_dir.rglob("*.youtube-segment.json")) if segments_dir.exists() else 0
    return YouTubeSegmentFtsBuildStats(
        db_path=db_path,
        segments_seen=seen,
        segments_indexed=indexed,
        segments_skipped=max(0, seen - indexed),
    )


def search_youtube_segments(
    query: str,
    top_k: int | None = 50,
    db_path: Path = config.CARD_FTS_DB_PATH,
) -> list[YouTubeSegmentFtsMatch]:
    """Search every matching YouTube segment; ``top_k=None`` means all."""
    match_query = _to_fts_query(query)
    if not match_query or not db_path.exists():
        return []
    limit = None if top_k is None else max(1, top_k)
    with closing(_connect_read_only(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT segment_id, parent_source_id, video_id, segment_index,
                   start_seconds, end_seconds, start_url, card_path, title,
                   bm25(youtube_segments_fts) AS rank,
                   snippet(youtube_segments_fts, 9, '...', '...', ' ', 24) AS snippet
            FROM youtube_segments_fts
            WHERE youtube_segments_fts MATCH ?
            ORDER BY rank, segment_index, segment_id
        """
        params: tuple[object, ...] = (match_query,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        rows = conn.execute(sql, params).fetchall()
        return [
            YouTubeSegmentFtsMatch(
                segment_id=row["segment_id"],
                parent_source_id=row["parent_source_id"],
                video_id=row["video_id"],
                segment_index=int(row["segment_index"]),
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                start_url=row["start_url"] or "",
                card_path=row["card_path"],
                title=row["title"],
                score=round(-float(row["rank"]), 6),
                snippet=_clean_snippet(row["snippet"]),
            )
            for row in rows
        ]


def list_youtube_segment_ids(
    parent_source_id: str,
    db_path: Path = config.CARD_FTS_DB_PATH,
) -> set[str]:
    """Return every indexed segment ID for one YouTube episode."""
    if not db_path.exists():
        return set()
    with closing(_connect_read_only(db_path)) as conn:
        rows = conn.execute(
            "SELECT segment_id FROM youtube_segments_fts WHERE parent_source_id = ?",
            (parent_source_id,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def iter_card_records(enriched_dir: Path = config.ENRICHED_DIR) -> Iterable[CardFtsRecord]:
    if not enriched_dir.exists():
        return

    for card_path in sorted(enriched_dir.rglob("*.enriched.json")):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Card FTS could not read %s: %s", card_path, exc)
            continue
        if card.get("duplicate_of"):
            # The root card of the same content is indexed instead.
            continue
        record = card_to_fts_record(card, card_path)
        if record:
            yield record


def iter_youtube_segment_records(
    segments_dir: Path = config.YOUTUBE_SEGMENTS_DIR,
) -> Iterable[tuple[object, ...]]:
    if not segments_dir.exists():
        return
    for card_path in sorted(segments_dir.rglob("*.youtube-segment.json")):
        try:
            segment = YouTubeSegmentCardV2.model_validate(
                json.loads(card_path.read_text(encoding="utf-8"))
            )
            search_text = segment.search_text.strip()
            if not search_text or segment.duplicate_of:
                continue
            yield (
                segment.segment_id,
                segment.parent_source_id,
                segment.video_id,
                segment.segment_index,
                segment.start_seconds,
                segment.end_seconds,
                segment.start_url,
                str(card_path),
                segment.title or "YouTube",
                search_text,
            )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            logger.debug("YouTube segment FTS could not read %s: %s", card_path, exc)


def card_to_fts_record(card: dict[str, Any], card_path: Path) -> CardFtsRecord | None:
    if card.get("schema_version") != "enriched_v2":
        return None
    provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    search_text = card_search_text(card, card_path)
    if not search_text:
        return None

    normalized_file = _clean_str(provenance.get("normalized_path"))
    channel_name = _clean_str(provenance.get("channel")) or "?"
    date = _clean_str(provenance.get("date"))
    title = _clean_str(provenance.get("source_title")) or f"{channel_name} - {date[:10] if date else '?'}"

    return CardFtsRecord(
        source_id=_v2_source_id(card),
        card_path=str(card_path),
        normalized_file=normalized_file,
        post_url=_clean_str(provenance.get("post_url")),
        title=title,
        search_text=search_text,
        entities=_flatten_entities(card.get("entities")),
        topics=_join_texts(card.get("topics")),
        claim_types=_claim_types(card),
    )


def _v2_source_id(card: dict[str, Any]) -> str:
    provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    return _clean_str(provenance.get("source_id"))


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
            source_id UNINDEXED,
            card_path UNINDEXED,
            normalized_file UNINDEXED,
            post_url UNINDEXED,
            title,
            search_text,
            entities,
            topics,
            claim_types,
            tokenize='unicode61'
        )
        """
    )


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open an existing FTS database without allowing query-time mutations."""
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _create_youtube_segment_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS youtube_segments_fts USING fts5(
            segment_id UNINDEXED,
            parent_source_id UNINDEXED,
            video_id UNINDEXED,
            segment_index UNINDEXED,
            start_seconds UNINDEXED,
            end_seconds UNINDEXED,
            start_url UNINDEXED,
            card_path UNINDEXED,
            title,
            search_text,
            tokenize='unicode61'
        )
        """
    )


def _to_fts_query(query: str) -> str:
    terms = _query_terms(query)
    return " OR ".join(_fts_term(term) for term in terms)


def _fts_term(term: str) -> str:
    escaped = term.replace('"', '""')
    if " " in escaped:
        return f'"{escaped}"'
    return f'"{escaped}"*'


def _query_terms(query: str) -> list[str]:
    terms = []
    seen = set()
    for term in expand_query_terms(_TOKEN_RE.findall(query.casefold())):
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _coverage_score(search_text: str, query_terms: list[str]) -> int:
    ranking_text = strip_metadata_lines(search_text)
    text_tokens = add_compound_terms(_COVERAGE_TOKEN_RE.findall(ranking_text.casefold()))
    return sum(1 for term in query_terms if _term_matches_tokens(text_tokens, term))


def _term_matches_tokens(text_tokens: list[str], term: str) -> bool:
    phrase_tokens = _COVERAGE_TOKEN_RE.findall(term.casefold())
    if not phrase_tokens:
        return False
    if len(phrase_tokens) == 1:
        return any(_matches_term(token, phrase_tokens[0]) for token in text_tokens)
    width = len(phrase_tokens)
    return any(
        all(
            _matches_term(token, expected)
            for token, expected in zip(window, phrase_tokens, strict=True)
        )
        for start in range(len(text_tokens) - width + 1)
        for window in (text_tokens[start : start + width],)
    )


def _matches_term(token: str, term: str) -> bool:
    return matches_term(token, term)


def _count_enriched_cards(enriched_dir: Path) -> int:
    if not enriched_dir.exists():
        return 0
    return sum(1 for _ in enriched_dir.rglob("*.enriched.json"))


def _flatten_entities(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    items: list[str] = []
    for group in value.values():
        if isinstance(group, list):
            for item in group:
                if isinstance(item, dict):
                    text = str(item.get("text", "")).strip()
                else:
                    text = str(item).strip()
                if text:
                    items.append(text)
        elif str(group).strip():
            items.append(str(group))
    return " ".join(items)


def _claim_types(card: dict[str, Any]) -> str:
    values = []
    for point in card.get("key_points") or []:
        if isinstance(point, dict):
            point_type = _clean_str(point.get("type"))
            if point_type:
                values.append(point_type)
    return " ".join(sorted(set(values)))


def _join_texts(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("label", "") or item.get("text", "")).strip()
            else:
                text = str(item).strip()
            if text:
                parts.append(text)
        return " ".join(parts)
    if str(value).strip():
        return str(value)
    return ""


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clean_snippet(value: Any) -> str:
    text = _clean_str(value).replace("\n", " ")
    return re.sub(r"\s+", " ", text)
