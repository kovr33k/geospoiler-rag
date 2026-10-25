"""LightRAG storage paths, document ids, metadata, and rebuild helpers."""

import asyncio
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from lightrag import LightRAG
from lightrag.utils import compute_mdhash_id

from geospoiler import config
from geospoiler.providers.runtime import logger

_HEADER_LINE_RE = re.compile(r"^\[(?=.*(?:Канал:|Дата:|Пост:)).*\]\s*$")

def _source_index_path() -> Path:
    """Resolve the SQLite metadata index path from the current config at runtime."""
    return config.RAG_STORAGE_DIR / "doc_metadata_index.sqlite"


def _legacy_source_index_path() -> Path:
    """Resolve the retired JSON metadata index path for one-way migration."""
    return config.RAG_STORAGE_DIR / "doc_metadata_index.json"


def _skipped_insert_report_path() -> Path:
    """Resolve the per-load skipped insert report path."""
    return config.PROJECT_ROOT / "artifacts" / "rag_insert_skipped.md"


def load_source_metadata_index() -> dict[str, dict[str, Any]]:
    """Load the persisted source metadata lookup as a dict for compatibility."""
    _migrate_legacy_source_metadata_index()
    index_path = _source_index_path()
    if not index_path.exists():
        return {}
    conn = _connect_source_metadata_index()
    try:
        rows = conn.execute(
            "SELECT source_path, metadata_json FROM source_metadata ORDER BY source_path"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    data: dict[str, dict[str, Any]] = {}
    for source_path, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict):
            # Keep basename aliases in SQLite for LightRAG provenance lookup,
            # but do not expose those implementation aliases through the
            # compatibility mapping.  Otherwise one logical document appears
            # multiple times to callers that use this helper for counts.
            canonical_path = metadata.get("canonical_path")
            if (
                canonical_path
                and str(source_path) == Path(str(canonical_path)).name
                and not str(source_path).startswith("__geospoiler__")
            ):
                continue
            data[str(source_path)] = metadata
    return data if isinstance(data, dict) else {}


def _connect_source_metadata_index() -> sqlite3.Connection:
    """Open the SQLite metadata index and ensure the schema exists."""
    index_path = _source_index_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_metadata (
            source_path TEXT PRIMARY KEY,
            metadata_json TEXT NOT NULL
        )
        """
    )
    return conn


def _migrate_legacy_source_metadata_index() -> None:
    """Import the retired JSON index into SQLite once when SQLite is absent."""
    index_path = _source_index_path()
    if index_path.exists():
        return
    legacy_path = _legacy_source_index_path()
    if not legacy_path.exists():
        return
    try:
        legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(legacy_data, dict):
        return

    conn = _connect_source_metadata_index()
    try:
        for source_path, metadata in legacy_data.items():
            if not isinstance(metadata, dict):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO source_metadata (source_path, metadata_json)
                VALUES (?, ?)
                """,
                (str(source_path), json.dumps(metadata, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()

def _parse_header_metadata(text: str) -> dict:
    """Best-effort metadata extraction from the first line of a normalized document."""
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if not _HEADER_LINE_RE.match(first_line):
        return {}
    inner = first_line.strip()[1:-1]
    metadata: dict[str, str] = {}
    for part in inner.split(" | "):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _load_document_metadata(source_path: str, text: str) -> dict:
    """Load sidecar metadata when available, falling back to parsed header metadata."""
    source_file = config.resolve_project_path(source_path)
    metadata_path = source_file.with_suffix(".meta.json")
    if metadata_path.exists():
        try:
            return json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(f"  Failed to read metadata sidecar for {source_path}")
    return _parse_header_metadata(text)


def _sync_source_metadata_index(source_path: str, metadata: dict) -> None:
    """Persist source metadata in the SQLite lookup index inside rag_storage."""
    _migrate_legacy_source_metadata_index()
    conn = _connect_source_metadata_index()
    canonical_path = _canonical_source_path(source_path)
    aliases = {canonical_path, _canonical_source_path(_rag_file_path(canonical_path))}
    basename = Path(canonical_path).name
    # LightRAG may persist a basename even when an absolute path was supplied.
    # Keep that alias only while it is globally unambiguous; duplicate topic
    # directories must use the deterministic virtual path instead.
    existing_basename = conn.execute(
        "SELECT metadata_json FROM source_metadata WHERE source_path = ?",
        (basename,),
    ).fetchone()
    if existing_basename is None:
        aliases.add(basename)
    else:
        try:
            existing_metadata = json.loads(existing_basename[0])
        except (TypeError, json.JSONDecodeError):
            existing_metadata = {}
        if existing_metadata.get("canonical_path") == canonical_path:
            aliases.add(basename)
        else:
            conn.execute("DELETE FROM source_metadata WHERE source_path = ?", (basename,))
    index_metadata = dict(metadata)
    index_metadata.setdefault("canonical_path", canonical_path)
    try:
        encoded = json.dumps(index_metadata, ensure_ascii=False)
        conn.executemany(
            "INSERT OR REPLACE INTO source_metadata (source_path, metadata_json) VALUES (?, ?)",
            [(alias, encoded) for alias in sorted(aliases)],
        )
        conn.commit()
    finally:
        conn.close()


def _remove_source_metadata_index(source_path: str) -> None:
    """Remove every persisted alias for a document that did not finish insertion.

    LightRAG's terminal status is diagnostic only: it may report a basename or
    a virtual citation path instead of the path originally submitted.  The
    metadata index is therefore reconciled by its runner-owned
    ``canonical_path`` value, not by the status path.
    """
    _migrate_legacy_source_metadata_index()
    index_path = _source_index_path()
    if not index_path.exists():
        return
    canonical_path = _canonical_source_path(source_path)
    paths_to_remove = {
        canonical_path,
        _canonical_source_path(_rag_file_path(canonical_path)),
    }
    conn = _connect_source_metadata_index()
    try:
        rows = conn.execute(
            "SELECT source_path, metadata_json FROM source_metadata"
        ).fetchall()
        for stored_path, metadata_json in rows:
            try:
                metadata = json.loads(metadata_json)
            except (TypeError, json.JSONDecodeError):
                metadata = None
            if isinstance(metadata, dict):
                stored_canonical = metadata.get("canonical_path")
                if isinstance(stored_canonical, str) and stored_canonical:
                    try:
                        if _canonical_source_path(stored_canonical) == canonical_path:
                            paths_to_remove.add(str(stored_path))
                    except (OSError, RuntimeError, ValueError):
                        pass
        conn.executemany(
            "DELETE FROM source_metadata WHERE source_path = ?",
            [(path,) for path in sorted(paths_to_remove)],
        )
        conn.commit()
    finally:
        conn.close()


def _write_skipped_insert_report(skipped: list[dict[str, Any]]) -> None:
    """Persist the final list of documents skipped during RAG insertion."""
    report_path = _skipped_insert_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Skipped RAG Inserts",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Skipped: {len(skipped)}",
        "",
    ]

    if not skipped:
        lines.append("No documents were skipped during the last load.")
    else:
        lines.extend(
            [
                "| # | Reason | Length | Source |",
                "|---:|---|---:|---|",
            ]
        )
        for idx, item in enumerate(skipped, start=1):
            reason = str(item.get("reason", "")).replace("|", "\\|")
            source_path = str(item.get("source_path", "")).replace("|", "\\|")
            content_length = item.get("content_length", "")
            lines.append(f"| {idx} | {reason} | {content_length} | `{source_path}` |")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _cleanup_skipped_doc(rag: LightRAG, doc_id: str) -> None:
    """Remove a timed-out document from active LightRAG queues before continuing."""
    try:
        deletion = await asyncio.wait_for(
            rag.adelete_by_doc_id(doc_id),
            timeout=config.RAG_DELETE_TIMEOUT_SECONDS,
        )
        if getattr(deletion, "status", "") in {"success", "not_found"}:
            return
        logger.warning(
            "  Cleanup for skipped doc_id=%s returned %s: %s",
            doc_id,
            getattr(deletion, "status", "unknown"),
            getattr(deletion, "message", ""),
        )
    except Exception as exc:
        logger.warning(f"  Cleanup via adelete_by_doc_id failed for {doc_id}: {exc}")

    # Last-resort cleanup prevents a PROCESSING/FAILED status from being picked up
    # again by the next insert. Rebuild starts from empty storage, so any partial
    # graph data from this document stays isolated to the failed insert attempt.
    for storage_name in (
        "doc_status",
        "full_docs",
        "text_chunks",
        "full_entities",
        "full_relations",
        "entity_chunks",
        "relation_chunks",
        "vdb_entities",
        "vdb_relationships",
    ):
        storage = getattr(rag, storage_name, None)
        delete = getattr(storage, "delete", None)
        if delete is None:
            continue
        try:
            await delete([doc_id])
        except Exception as exc:
            logger.warning(f"  Direct cleanup failed for {storage_name}/{doc_id}: {exc}")


def _doc_status_value(status_doc: Any) -> str:
    if not status_doc:
        return ""
    if isinstance(status_doc, dict):
        value = status_doc.get("status", "")
    else:
        value = getattr(status_doc, "status", "")
    # LightRAG returns DocStatus enum instances in the live API; str(enum)
    # is "DocStatus.PROCESSED", while its persisted value is "processed".
    return str(getattr(value, "value", value)).rsplit(".", 1)[-1]


def _doc_status_field(status_doc: Any, field_name: str, default: Any = "") -> Any:
    if not status_doc:
        return default
    if isinstance(status_doc, dict):
        return status_doc.get(field_name, default)
    return getattr(status_doc, field_name, default)


async def _wait_for_doc_terminal_status(
    rag: LightRAG,
    doc_id: str,
    timeout_seconds: float,
) -> Any:
    """Wait until LightRAG has actually finished processing the inserted doc."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_status_doc: Any = None
    pending_statuses = {"pending", "processing", "preprocessed"}

    while True:
        last_status_doc = await rag.doc_status.get_by_id(doc_id)
        status = _doc_status_value(last_status_doc).lower()
        if status in {"processed", "failed"}:
            return last_status_doc
        if status and status not in pending_statuses:
            return last_status_doc
        if not status:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            await asyncio.sleep(1.0)
            continue

        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        await asyncio.sleep(1.0)

def rebuild_rag_storage(preserve_llm_cache: bool = False) -> Path | None:
    """
    Archive the current LightRAG storage directory and recreate it empty.

    Args:
        preserve_llm_cache: If True, copies the LLM response cache back to the fresh storage.

    Returns:
        Path to the created backup directory, or None if there was nothing to back up.
    """
    storage_dir = config.RAG_STORAGE_DIR
    backup_root = config.PROJECT_ROOT / "rag_storage_backups"
    backup_root.mkdir(parents=True, exist_ok=True)

    if not storage_dir.exists() or not any(storage_dir.iterdir()):
        storage_dir.mkdir(parents=True, exist_ok=True)
        logger.info("RAG storage is already empty; nothing to back up.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_root / f"{storage_dir.name}_{timestamp}"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_root / f"{storage_dir.name}_{timestamp}_{suffix}"
        suffix += 1

    shutil.move(str(storage_dir), str(backup_path))
    storage_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Archived RAG storage to: {backup_path}")

    if preserve_llm_cache:
        cache_name = "kv_store_llm_response_cache.json"
        cache_source = backup_path / cache_name
        if cache_source.exists():
            shutil.copy2(str(cache_source), str(storage_dir / cache_name))
            logger.info("Preserved LLM response cache for embedding rebuild.")

    return backup_path


def _canonical_source_path(source_path: str) -> str:
    """Normalize a source path so one logical source maps to one LightRAG doc_id."""
    return str(config.logical_project_path(source_path))


def _source_doc_id(source_path: str) -> str:
    """Build a stable document ID from the normalized source path."""
    return compute_mdhash_id(_canonical_source_path(source_path), prefix="doc-")


def _rag_file_path(source_path: str) -> str:
    """Return the citation path passed to LightRAG.

    LightRAG deliberately canonicalizes ``file_paths`` to basenames.  That is
    unsafe for this corpus because every normalized topic directory can have a
    ``1.txt``, ``21.txt``, etc.  Keep the real path when its basename is unique;
    otherwise use a deterministic virtual basename.  The source metadata index
    maps that virtual name back to the real normalized path.
    """
    canonical_path = _canonical_source_path(source_path)
    basename = Path(canonical_path).name

    try:
        normalized_root = config.NORMALIZED_DIR.resolve(strict=False)
        config.resolve_project_path(canonical_path).relative_to(normalized_root)
        matching_paths = [
            path
            for path in config.NORMALIZED_DIR.rglob(basename)
            if path.is_file()
        ]
    except (OSError, ValueError):
        matching_paths = []

    if len(matching_paths) <= 1:
        return canonical_path

    suffix = Path(basename).suffix or ".txt"
    return f"__geospoiler__{_source_doc_id(canonical_path)}{suffix}"
