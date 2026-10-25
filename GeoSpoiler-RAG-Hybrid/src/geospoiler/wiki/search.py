"""FTS-backed Wiki resolver with stable source references for RAG."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from geospoiler.wiki.projections import get_projection_artifact

DocumentKind = Literal["card", "claim", "hub"]


@dataclass(frozen=True)
class WikiSearchMatch:
    document_kind: DocumentKind
    scope_key: str
    title: str
    snippet: str
    rank: float
    source_refs: object
    projection_output_hash: str


@dataclass(frozen=True)
class WikiResolvedContext:
    query: str
    matches: tuple[WikiSearchMatch, ...]
    context_text: str
    source_refs: tuple[object, ...]


def search_wiki(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 12,
    document_kinds: tuple[DocumentKind, ...] = ("hub", "claim", "card"),
) -> tuple[WikiSearchMatch, ...]:
    """Search generated Wiki state; unknown/unapproved cards remain searchable as cards."""
    match_expression = _fts_query(query)
    if not match_expression or limit <= 0 or not document_kinds:
        return ()
    allowed = {"card", "claim", "hub"}
    if any(kind not in allowed for kind in document_kinds):
        raise ValueError("Unsupported Wiki document kind")
    placeholders = ", ".join("?" for _ in document_kinds)
    rows = connection.execute(
        f"""
        SELECT
            document.document_kind,
            document.scope_key,
            document.title,
            snippet(wiki_fts, 1, '[', ']', ' … ', 24) AS snippet_text,
            bm25(wiki_fts, 4.0, 1.0) AS rank_value,
            document.source_ref_json,
            document.projection_output_hash
        FROM wiki_fts
        JOIN wiki_fts_documents AS document
          ON document.rowid = wiki_fts.rowid
        WHERE wiki_fts MATCH ?
          AND document.document_kind IN ({placeholders})
        ORDER BY rank_value, document.document_kind, document.scope_key
        LIMIT ?
        """,
        (match_expression, *document_kinds, int(limit)),
    ).fetchall()
    return tuple(
        WikiSearchMatch(
            document_kind=row["document_kind"],
            scope_key=row["scope_key"],
            title=row["title"],
            snippet=row["snippet_text"],
            rank=float(row["rank_value"]),
            source_refs=json.loads(row["source_ref_json"]),
            projection_output_hash=row["projection_output_hash"],
        )
        for row in rows
    )


def resolve_wiki_context(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 8,
    max_chars: int = 20_000,
) -> WikiResolvedContext:
    """Resolve top hubs/claims/cards into bounded context with auditable sources."""
    matches = search_wiki(connection, query, limit=limit)
    blocks: list[str] = []
    source_refs: list[object] = []
    used = 0
    for match in matches:
        artifact = get_projection_artifact(
            connection,
            projection_kind=match.document_kind,
            scope_key=match.scope_key,
        )
        if artifact is None:
            continue
        block = (
            f"<!-- wiki:{match.document_kind}:{match.scope_key} -->\n"
            f"{artifact.rendered_content}"
        )
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip() + "\n"
        blocks.append(block)
        used += len(block)
        source_refs.append(match.source_refs)
    return WikiResolvedContext(
        query=query,
        matches=matches,
        context_text="\n\n".join(blocks),
        source_refs=tuple(source_refs),
    )


def _fts_query(value: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if token
    ]
    # Prefix + OR keeps FTS useful across Russian inflections; bm25 still ranks
    # documents matching more of the thoughtful multi-word query first.
    return " OR ".join(f'"{token}"*' for token in tokens[:24])
