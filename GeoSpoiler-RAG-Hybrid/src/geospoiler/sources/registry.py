import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.contracts.models import SourceId

logger = logging.getLogger("geospoiler.retrieval.source_registry")

_CONTENT_HASH_FIELDS = (
    "provenance",
    "content_type",
    "language",
    "summary",
    "key_points",
    "entities",
    "topics",
    "quotes",
    "events",
    "source_chain",
    "graph_text",
    "search_text",
)


@dataclass(frozen=True)
class SourceRegistryStats:
    db_path: Path
    run_id: str
    sources: int
    normalized_docs: int
    enriched_cards: int
    references: int


@dataclass(frozen=True)
class SourcePassport:
    source_id: str
    post_url: str
    primary_url: str
    normalized_file: str
    meta_file: str
    card_path: str
    channel_name: str
    channel_id: str
    message_id: str
    date: str
    content_type: str
    language: str
    youtube_url: str
    original_source: str


def rebuild_source_registry(
    normalized_dir: Path = config.NORMALIZED_DIR,
    enriched_dir: Path = config.ENRICHED_DIR,
    db_path: Path = config.SOURCE_REGISTRY_DB_PATH,
) -> SourceRegistryStats:
    """Rebuild the local source registry from normalized metadata and enriched cards."""
    # A resumed or rapid consecutive run can rebuild twice within one second.
    run_id = datetime.now(UTC).isoformat()
    sources: dict[str, dict[str, str]] = {}
    normalized_rows: list[dict[str, str]] = []
    enriched_rows: list[dict[str, str]] = []
    reference_rows: list[dict[str, str]] = []

    for meta_path, meta in _iter_normalized_meta(normalized_dir):
        normalized_source_id = SourceId.from_provenance(meta)
        source_id = normalized_source_id.value if normalized_source_id else None
        if not source_id:
            continue
        normalized_file = _normalized_file_for_meta(meta_path)
        source = _source_from_metadata(source_id, meta)
        source["normalized_file"] = _clean_str(normalized_file)
        source["meta_file"] = str(meta_path)
        _merge_source(sources, source_id, source)
        normalized_rows.append(
            {
                "source_id": source_id,
                "normalized_file": _clean_str(normalized_file),
                "meta_file": str(meta_path),
                "has_text": _bool_text(meta.get("has_text")),
                "has_images": _bool_text(meta.get("has_images")),
                "has_video": _bool_text(meta.get("has_video")),
                "has_voice": _bool_text(meta.get("has_voice")),
                "has_document": _bool_text(meta.get("has_document")),
            }
        )
        reference_rows.extend(_references_from_normalized_meta(source_id, meta))

    for card_path, card in _iter_enriched_cards(enriched_dir):
        provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
        source_id = _clean_str(provenance.get("source_id"))
        if not source_id:
            continue
        source_chain = card.get("source_chain") if isinstance(card.get("source_chain"), dict) else {}
        source = _source_from_enriched_provenance(source_id, provenance)
        source.update(
            {
                "card_path": str(card_path),
                "normalized_file": _clean_str(provenance.get("normalized_path")),
                "meta_file": "",
                "content_type": _clean_str(card.get("content_type")),
                "language": _clean_str(card.get("language")),
                "youtube_url": _extract_youtube_url(source_chain),
                "original_source": _clean_str(source_chain.get("original_source")),
            }
        )
        source["primary_url"] = source.get("youtube_url") or source.get("post_url", "")
        _merge_source(sources, source_id, source)
        enriched_rows.append(
            {
                "source_id": source_id,
                "card_path": str(card_path),
                "triage": "",
                "content_type": _clean_str(card.get("content_type")),
                "language": _clean_str(card.get("language")),
                "summary": _clean_str(card.get("summary")),
                "content_hash": _compute_content_hash(card),
            }
        )
        reference_rows.extend(_references_from_enriched_card(source_id, card))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        _create_schema(conn)
        _clear_registry(conn)
        _insert_sources(conn, sources)
        _insert_normalized_docs(conn, normalized_rows)
        _insert_enriched_cards(conn, enriched_rows)
        _insert_references(conn, reference_rows)
        conn.execute(
            """
            INSERT INTO processing_runs (
                run_id,
                started_at,
                completed_at,
                sources_count,
                normalized_docs_count,
                enriched_cards_count,
                references_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_id,
                _utc_now(),
                len(sources),
                len(normalized_rows),
                len(enriched_rows),
                len(reference_rows),
            ),
        )
        conn.commit()

    return SourceRegistryStats(
        db_path=db_path,
        run_id=run_id,
        sources=len(sources),
        normalized_docs=len(normalized_rows),
        enriched_cards=len(enriched_rows),
        references=len(reference_rows),
    )


def resolve_source(
    source_id: str,
    db_path: Path = config.SOURCE_REGISTRY_DB_PATH,
) -> SourcePassport | None:
    if not source_id or not db_path.exists():
        return None
    with closing(_connect_read_only(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT
                    source_id,
                    post_url,
                    primary_url,
                    normalized_file,
                    meta_file,
                    card_path,
                    channel_name,
                    channel_id,
                    message_id,
                    date,
                    content_type,
                    language,
                    youtube_url,
                    original_source
                FROM sources
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        return _source_passport_from_row(row)


def resolve_source_path(
    file_path: str,
    db_path: Path = config.SOURCE_REGISTRY_DB_PATH,
    metadata_index_path: Path | None = None,
) -> SourcePassport | None:
    """Resolve a LightRAG provenance path without unsafe basename matching.

    LightRAG may return a canonical normalized path, a deterministic
    ``__geospoiler__doc-*`` virtual name, or a path with different separators
    and casing.  Virtual names are resolved only through the metadata index
    written during ingestion; ordinary paths are matched against the complete
    normalized path.  A filename by itself is deliberately never searched in
    the registry, because duplicate topic directories are a normal corpus
    condition.
    """
    if not str(file_path or "").strip() or not db_path.exists():
        return None

    target_keys = _path_lookup_keys(file_path)
    canonical_from_metadata = _canonical_path_from_metadata_index(
        file_path,
        metadata_index_path=metadata_index_path,
    )
    if canonical_from_metadata:
        target_keys.update(_path_lookup_keys(canonical_from_metadata))

    if not target_keys:
        return None

    with closing(_connect_read_only(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                    source_id,
                    post_url,
                    primary_url,
                    normalized_file,
                    meta_file,
                    card_path,
                    channel_name,
                    channel_id,
                    message_id,
                    date,
                    content_type,
                    language,
                    youtube_url,
                    original_source
                FROM sources
                WHERE normalized_file != ''
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return None

    matches = [
        row
        for row in rows
        if target_keys.intersection(_path_lookup_keys(_clean_str(row["normalized_file"])))
    ]
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning("Ambiguous LightRAG provenance path ignored: %s", file_path)
        return None
    return _source_passport_from_row(matches[0])


def _source_passport_from_row(row: sqlite3.Row) -> SourcePassport:
    return SourcePassport(**{key: _clean_str(row[key]) for key in row.keys()})


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open the existing registry in read-only mode for query resolution."""
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def _path_lookup_keys(value: str) -> set[str]:
    """Return full-path keys only; this helper must not produce basename keys."""
    raw = _clean_str(value)
    if not raw:
        return set()

    keys = {_normalise_path_key(raw)}
    try:
        path = config.resolve_project_path(raw)
        keys.add(_normalise_path_key(str(path.resolve(strict=False))))
        keys.add(_normalise_path_key(str(config.logical_project_path(path))))
    except (OSError, ValueError):
        pass
    return {key for key in keys if key}


def _normalise_path_key(value: str) -> str:
    return _clean_str(value).replace("\\", "/").casefold()


def _canonical_path_from_metadata_index(
    file_path: str,
    *,
    metadata_index_path: Path | None,
) -> str:
    """Look up an explicit ingestion mapping for a virtual LightRAG path."""
    index_path = metadata_index_path or (config.RAG_STORAGE_DIR / "doc_metadata_index.sqlite")
    if not index_path.exists():
        return ""

    keys = _path_lookup_keys(file_path)
    if not keys:
        return ""
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_read_only(index_path)
        rows = conn.execute(
            "SELECT source_path, metadata_json FROM source_metadata"
        ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        if conn is not None:
            conn.close()

    matches: list[str] = []
    raw_input = Path(_clean_str(file_path))
    basename_only = not raw_input.is_absolute() and len(raw_input.parts) == 1
    for source_path, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            continue
        canonical_path = _clean_str(metadata.get("canonical_path")) if isinstance(metadata, dict) else ""
        exact_index_match = bool(
            keys.intersection(_path_lookup_keys(_clean_str(source_path)))
        )
        unique_basename_candidate = bool(
            basename_only
            and canonical_path
            and Path(canonical_path).name.casefold() == raw_input.name.casefold()
        )
        if (exact_index_match or unique_basename_candidate) and canonical_path:
            matches.append(canonical_path)

    unique = sorted(set(matches), key=str.casefold)
    return unique[0] if len(unique) == 1 else ""


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            channel_name TEXT NOT NULL DEFAULT '',
            channel_id TEXT NOT NULL DEFAULT '',
            channel_username TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL DEFAULT '',
            post_url TEXT NOT NULL DEFAULT '',
            primary_url TEXT NOT NULL DEFAULT '',
            normalized_file TEXT NOT NULL DEFAULT '',
            meta_file TEXT NOT NULL DEFAULT '',
            card_path TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            youtube_url TEXT NOT NULL DEFAULT '',
            original_source TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS normalized_docs (
            source_id TEXT PRIMARY KEY,
            normalized_file TEXT NOT NULL DEFAULT '',
            meta_file TEXT NOT NULL DEFAULT '',
            has_text INTEGER NOT NULL DEFAULT 0,
            has_images INTEGER NOT NULL DEFAULT 0,
            has_video INTEGER NOT NULL DEFAULT 0,
            has_voice INTEGER NOT NULL DEFAULT 0,
            has_document INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(source_id) REFERENCES sources(source_id)
        );

        CREATE TABLE IF NOT EXISTS enriched_cards (
            source_id TEXT PRIMARY KEY,
            card_path TEXT NOT NULL DEFAULT '',
            triage TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(source_id) REFERENCES sources(source_id)
        );

        CREATE TABLE IF NOT EXISTS processing_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            sources_count INTEGER NOT NULL DEFAULT 0,
            normalized_docs_count INTEGER NOT NULL DEFAULT 0,
            enriched_cards_count INTEGER NOT NULL DEFAULT 0,
            references_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS "references" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            reference_type TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(source_id) REFERENCES sources(source_id)
        );
        """
    )


def _clear_registry(conn: sqlite3.Connection) -> None:
    conn.execute('DELETE FROM "references"')
    conn.execute("DELETE FROM enriched_cards")
    conn.execute("DELETE FROM normalized_docs")
    conn.execute("DELETE FROM sources")


def _insert_sources(conn: sqlite3.Connection, sources: dict[str, dict[str, str]]) -> None:
    conn.executemany(
        """
        INSERT INTO sources (
            source_id,
            channel_name,
            channel_id,
            channel_username,
            message_id,
            date,
            post_url,
            primary_url,
            normalized_file,
            meta_file,
            card_path,
            content_type,
            language,
            youtube_url,
            original_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                source_id,
                source.get("channel_name", ""),
                source.get("channel_id", ""),
                source.get("channel_username", ""),
                source.get("message_id", ""),
                source.get("date", ""),
                source.get("post_url", ""),
                source.get("primary_url", ""),
                source.get("normalized_file", ""),
                source.get("meta_file", ""),
                source.get("card_path", ""),
                source.get("content_type", ""),
                source.get("language", ""),
                source.get("youtube_url", ""),
                source.get("original_source", ""),
            )
            for source_id, source in sorted(sources.items())
        ],
    )


def _insert_normalized_docs(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        """
        INSERT INTO normalized_docs (
            source_id,
            normalized_file,
            meta_file,
            has_text,
            has_images,
            has_video,
            has_voice,
            has_document
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["source_id"],
                row["normalized_file"],
                row["meta_file"],
                int(row["has_text"]),
                int(row["has_images"]),
                int(row["has_video"]),
                int(row["has_voice"]),
                int(row["has_document"]),
            )
            for row in rows
        ],
    )


def _insert_enriched_cards(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        """
        INSERT INTO enriched_cards (
            source_id,
            card_path,
            triage,
            content_type,
            language,
            summary,
            content_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["source_id"],
                row["card_path"],
                row["triage"],
                row["content_type"],
                row["language"],
                row["summary"],
                row["content_hash"],
            )
            for row in rows
        ],
    )


def _insert_references(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        """
        INSERT INTO "references" (
            source_id,
            reference_type,
            url,
            label,
            origin
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                row["source_id"],
                row["reference_type"],
                row["url"],
                row["label"],
                row["origin"],
            )
            for row in rows
        ],
    )


def _source_from_metadata(source_id: str, metadata: dict[str, Any]) -> dict[str, str]:
    post_url = _clean_str(metadata.get("post_url"))
    return {
        "source_id": source_id,
        "channel_name": _clean_str(metadata.get("channel_name")),
        "channel_id": _clean_str(metadata.get("channel_id")),
        "channel_username": _clean_str(metadata.get("channel_username")),
        "message_id": _clean_str(metadata.get("message_id")),
        "date": _clean_str(metadata.get("date")),
        "post_url": post_url,
        "primary_url": post_url,
    }


def _source_from_enriched_provenance(
    source_id: str,
    provenance: dict[str, Any],
) -> dict[str, str]:
    """Map strict v2 provenance names into the registry's stable DB columns."""
    post_url = _clean_str(provenance.get("post_url"))
    return {
        "source_id": source_id,
        "channel_name": _clean_str(provenance.get("channel")),
        "channel_id": "",
        "channel_username": "",
        "message_id": _clean_str(provenance.get("message_id")),
        "date": _clean_str(provenance.get("date")),
        "post_url": post_url,
        "primary_url": post_url,
    }


def _merge_source(sources: dict[str, dict[str, str]], source_id: str, incoming: dict[str, str]) -> None:
    current = sources.setdefault(source_id, {"source_id": source_id})
    for key, value in incoming.items():
        text = _clean_str(value)
        if text:
            current[key] = text
    if not current.get("primary_url"):
        current["primary_url"] = current.get("youtube_url") or current.get("post_url", "")


def _references_from_normalized_meta(source_id: str, meta: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field, reference_type in (
        ("youtube_urls", "youtube"),
        ("instagram_urls", "instagram"),
        ("ai_chat_urls", "ai_chat"),
        ("web_urls", "web"),
    ):
        for url in _iter_urls(meta.get(field)):
            rows.append(_reference_row(source_id, reference_type, url, field, "normalized_meta"))
    return rows


def _references_from_enriched_card(source_id: str, card: dict[str, Any]) -> list[dict[str, str]]:
    source_chain = card.get("source_chain") if isinstance(card.get("source_chain"), dict) else {}
    rows: list[dict[str, str]] = []
    for item in source_chain.get("external_links") or []:
        if not isinstance(item, dict):
            continue
        url = _clean_str(item.get("url"))
        label = _clean_str(item.get("label")) or "external_link"
        if url:
            reference_type = "youtube" if "youtube" in label.lower() else "external_link"
            rows.append(_reference_row(source_id, reference_type, url, label, "enriched_card"))
    return rows


def _reference_row(source_id: str, reference_type: str, url: str, label: str, origin: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "reference_type": reference_type,
        "url": url,
        "label": label,
        "origin": origin,
    }


def _iter_normalized_meta(normalized_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not normalized_dir.exists():
        return []
    rows = []
    for meta_path in sorted(normalized_dir.rglob("*.meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Source registry could not read normalized meta %s: %s", meta_path, exc)
            continue
        if isinstance(data, dict):
            rows.append((meta_path, data))
    return rows


def _iter_enriched_cards(enriched_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not enriched_dir.exists():
        return []
    rows = []
    for card_path in sorted(enriched_dir.rglob("*.enriched.json")):
        try:
            data = json.loads(card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Source registry could not read enriched card %s: %s", card_path, exc)
            continue
        if isinstance(data, dict) and data.get("schema_version") == "enriched_v2":
            rows.append((card_path, data))
    return rows


def _normalized_file_for_meta(meta_path: Path) -> Path:
    if meta_path.name.endswith(".meta.json"):
        return meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".txt")
    return meta_path.with_suffix(".txt")


def _iter_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean_str(item) for item in value if _clean_str(item)]
    text = _clean_str(value)
    return [text] if text else []


def _bool_text(value: Any) -> str:
    return "1" if bool(value) else "0"


def _extract_youtube_url(source_chain: dict) -> str:
    for link in source_chain.get("external_links") or []:
        if isinstance(link, dict) and "youtube" in (link.get("label") or "").lower():
            return _clean_str(link.get("url"))
    return ""


def _compute_content_hash(card: dict[str, Any]) -> str:
    """Hash source-registry metadata locally."""
    payload = {key: card.get(key) for key in _CONTENT_HASH_FIELDS if key in card}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
