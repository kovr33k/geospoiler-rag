"""Authoritative manual Markdown sidecars for approved Wiki concepts."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from geospoiler.wiki.hashing import content_hash, sha256_hex
from geospoiler.wiki.registry import RegistryError, get_concept, list_concepts
from geospoiler.wiki.state import get_dependency_head, publish_dependency

_FRONT_MATTER = re.compile(
    r"\A---\r?\nconcept_id:\s*(?P<concept_id>[^\r\n]+)\r?\n---(?:\r?\n|$)",
)


class SidecarError(RuntimeError):
    """Raised when a manual sidecar cannot be parsed or persisted safely."""


@dataclass(frozen=True)
class ManualSidecar:
    concept_id: str
    version_id: str | None
    generation: int
    content_hash: str
    markdown_text: str
    author: str
    changed: bool = False


@dataclass(frozen=True)
class SidecarSyncStats:
    files_seen: int
    sidecars_changed: int
    dependencies_changed: int
    files_written: int
    errors: tuple[str, ...] = ()


def sidecar_path(
    directory: str | Path,
    *,
    concept_id: str,
    canonical_label: str,
) -> Path:
    """Return a label-independent path that survives display-name changes."""
    _ = canonical_label
    digest = sha256_hex(concept_id)[:12]
    return Path(directory) / f"concept--{digest}.md"


def get_manual_sidecar(
    connection: sqlite3.Connection,
    concept_id: str,
) -> ManualSidecar:
    """Return the authoritative current sidecar, or an explicit empty value."""
    concept = get_concept(connection, concept_id)
    row = connection.execute(
        """
        SELECT
            sidecar.manual_sidecar_version_id,
            sidecar.sidecar_generation,
            sidecar.content_hash,
            sidecar.markdown_text,
            sidecar.author
        FROM manual_sidecar_heads AS head
        JOIN manual_sidecars AS sidecar
          ON sidecar.manual_sidecar_version_id =
             head.current_manual_sidecar_version_id
        WHERE head.concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    if row is None:
        markdown = ""
        return ManualSidecar(
            concept_id=concept.concept_id,
            version_id=None,
            generation=0,
            content_hash=_sidecar_hash(concept.concept_id, markdown),
            markdown_text=markdown,
            author="",
        )
    return ManualSidecar(
        concept_id=concept.concept_id,
        version_id=row["manual_sidecar_version_id"],
        generation=int(row["sidecar_generation"]),
        content_hash=row["content_hash"],
        markdown_text=row["markdown_text"],
        author=row["author"],
    )


def save_manual_sidecar(
    connection: sqlite3.Connection,
    *,
    concept_id: str,
    markdown_text: str,
    author: str = "user",
    directory: str | Path | None = None,
) -> ManualSidecar:
    """Append one authoritative sidecar version and optionally mirror it to disk."""
    concept = get_concept(connection, concept_id)
    markdown = _normalize_markdown(markdown_text)
    resolved_author = author.strip() or "user"
    new_hash = _sidecar_hash(concept_id, markdown)
    with _immediate_transaction(connection):
        current = get_manual_sidecar(connection, concept_id)
        if current.content_hash == new_hash:
            result = current
        else:
            generation = current.generation + 1
            version_id = (
                "manual-sidecar:v1:sha256:"
                + sha256_hex(
                    f"{concept_id}\n{generation}\n{new_hash}"
                )
            )
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO manual_sidecars (
                    manual_sidecar_version_id,
                    concept_id,
                    sidecar_generation,
                    content_hash,
                    markdown_text,
                    author,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    concept_id,
                    generation,
                    new_hash,
                    markdown,
                    resolved_author,
                    now,
                ),
            )
            if current.version_id is None:
                connection.execute(
                    """
                    INSERT INTO manual_sidecar_heads (
                        concept_id,
                        current_manual_sidecar_version_id,
                        current_sidecar_generation,
                        updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (concept_id, version_id, generation, now),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE manual_sidecar_heads
                    SET
                        current_manual_sidecar_version_id = ?,
                        current_sidecar_generation = ?,
                        updated_at = ?
                    WHERE concept_id = ?
                      AND current_manual_sidecar_version_id = ?
                    """,
                    (
                        version_id,
                        generation,
                        now,
                        concept_id,
                        current.version_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SidecarError("Manual sidecar head changed concurrently")
            result = ManualSidecar(
                concept_id=concept_id,
                version_id=version_id,
                generation=generation,
                content_hash=new_hash,
                markdown_text=markdown,
                author=resolved_author,
                changed=True,
            )

    publish_manual_sidecar_dependency(connection, concept_id)
    if directory is not None:
        _write_sidecar_file(
            sidecar_path(
                directory,
                concept_id=concept_id,
                canonical_label=concept.canonical_label,
            ),
            concept_id=concept_id,
            markdown_text=markdown,
        )
    return result


def publish_manual_sidecar_dependency(
    connection: sqlite3.Connection,
    concept_id: str,
) -> bool:
    """Publish the effective (including empty) sidecar as a narrow dependency."""
    sidecar = get_manual_sidecar(connection, concept_id)
    current = get_dependency_head(
        connection,
        dependency_kind="manual_sidecar",
        dependency_scope_key=concept_id,
    )
    published = publish_dependency(
        connection,
        dependency_kind="manual_sidecar",
        dependency_scope_key=concept_id,
        payload={
            "concept_id": concept_id,
            "sidecar_generation": sidecar.generation,
            "content_hash": sidecar.content_hash,
            "markdown_text": sidecar.markdown_text,
        },
        expected_version_id=(
            None if current is None else current.dependency_version_id
        ),
        producer_kind="manual",
    )
    return published.changed


def publish_all_manual_sidecar_dependencies(
    connection: sqlite3.Connection,
) -> int:
    """Ensure every approved concept has an explicit sidecar dependency."""
    return sum(
        int(publish_manual_sidecar_dependency(connection, concept.concept_id))
        for concept in list_concepts(connection)
    )


def sync_sidecars(
    connection: sqlite3.Connection,
    directory: str | Path,
    *,
    author: str = "filesystem",
) -> SidecarSyncStats:
    """Import edited sidecars, then materialize missing authoritative files."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    seen = 0
    changed = 0
    errors: list[str] = []
    for path in sorted(root.glob("*.md"), key=lambda item: item.name.casefold()):
        seen += 1
        try:
            concept_id, markdown = _read_sidecar_file(path)
            result = save_manual_sidecar(
                connection,
                concept_id=concept_id,
                markdown_text=markdown,
                author=author,
            )
            changed += int(result.changed)
        except (
            OSError,
            UnicodeError,
            RegistryError,
            SidecarError,
            ValueError,
        ) as exc:
            errors.append(f"{path}: {exc}")

    dependencies_changed = publish_all_manual_sidecar_dependencies(connection)
    written = 0
    for concept in list_concepts(connection):
        path = sidecar_path(
            root,
            concept_id=concept.concept_id,
            canonical_label=concept.canonical_label,
        )
        sidecar = get_manual_sidecar(connection, concept.concept_id)
        expected = _serialize_sidecar(
            concept_id=concept.concept_id,
            markdown_text=sidecar.markdown_text,
        )
        existing = (
            path.read_text(encoding="utf-8")
            if path.exists()
            else None
        )
        if existing != expected:
            _atomic_write_text(path, expected)
            written += 1
    return SidecarSyncStats(
        files_seen=seen,
        sidecars_changed=changed,
        dependencies_changed=dependencies_changed,
        files_written=written,
        errors=tuple(errors),
    )


def _read_sidecar_file(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise SidecarError("missing front matter with concept_id")
    concept_id = match.group("concept_id").strip()
    if not concept_id:
        raise SidecarError("empty concept_id")
    return concept_id, _normalize_markdown(raw[match.end() :])


def _write_sidecar_file(
    path: Path,
    *,
    concept_id: str,
    markdown_text: str,
) -> None:
    _atomic_write_text(
        path,
        _serialize_sidecar(
            concept_id=concept_id,
            markdown_text=markdown_text,
        ),
    )


def _serialize_sidecar(*, concept_id: str, markdown_text: str) -> str:
    body = _normalize_markdown(markdown_text)
    return f"---\nconcept_id: {concept_id}\n---\n{body}"


def _normalize_markdown(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{normalized}\n" if normalized else ""


def _sidecar_hash(concept_id: str, markdown_text: str) -> str:
    return content_hash(
        {
            "concept_id": concept_id,
            "markdown_text": markdown_text,
        },
        namespace="wiki-v2-manual-sidecar",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
