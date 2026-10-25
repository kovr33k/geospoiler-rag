"""Query/search CLI commands and source extraction helpers."""

import json
import re
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.answering.composer import search as composer_search
from geospoiler.answering.profiles import get_query_profile
from geospoiler.answering.query import query_rag_result
from geospoiler.cli.response_formatter import format_search_results
from geospoiler.cli.runtime import _finalize_rag_safely
from geospoiler.retrieval.lightrag.factory import create_rag
from geospoiler.retrieval.lightrag.storage import load_source_metadata_index

_SOURCE_REQUEST_RE = re.compile(
    r"(откуда|источник|источники|дай ссылк|ссылк|source|sources|citation|citations|where.*from)",
    re.IGNORECASE,
)
_REFERENCE_ID_RE = re.compile(r"\[reference_id:\s*([^\]]+)\]", re.IGNORECASE)
_REFERENCE_BULLET_RE = re.compile(r"^\s*-\s*\[(\d+)\]", re.MULTILINE)
_REFERENCE_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)\]")
_QUERY_MODES = {"local", "global", "hybrid", "naive", "mix", "bypass"}
_QUERY_PROFILES = {"answer", "source", "overview"}
_SEARCH_CARDS_ONLY_MODES = {"shadow", "cards", "cards-only"}


def _default_query_mode() -> str:
    """Prefer mix mode when reranking is enabled because it combines graph and chunk retrieval."""
    return "mix" if config.RERANKER_ENABLED else "hybrid"


async def cmd_query(question: str, mode: str | None = None, query_profile: str | None = None):
    """Query the LightRAG knowledge graph."""
    if mode is None:
        mode = _default_query_mode()
    if query_profile is None:
        query_profile = "source" if _question_requests_sources(question) else "answer"
    get_query_profile(query_profile)
    rag = await create_rag()
    query_result = None
    try:
        query_result = await query_rag_result(rag, question, mode=mode, query_profile=query_profile)
        answer = (
            query_result.get("llm_response", {}).get("content")
            if isinstance(query_result, dict)
            else None
        )
        if answer is None or not str(answer).strip() or str(answer).strip().lower() == "none":
            print("Query failed: LightRAG returned no answer. Check API connectivity and logs.")
            raise SystemExit(1)

        print("\n" + "═" * 60)
        print(f"Вопрос: {question}")
        print(f"Режим: {mode} (профиль: {query_profile})")
        print("Ответ:")
        print(answer)
        if query_profile == "source" or _question_requests_sources(question):
            _print_query_sources(query_result)
        print("═" * 60)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Query failed: {exc}")
        raise SystemExit(1) from exc
    finally:
        await _finalize_rag_safely(rag)


async def cmd_search(query: str, mode: str = "recall"):
    """Execute multi-index search via Retrieval Composer."""
    if mode.strip().lower() in _SEARCH_CARDS_ONLY_MODES:
        package = await composer_search(None, query, mode)
        report = format_search_results(package)
        print("\n" + "=" * 80)
        print(report)
        print("=" * 80 + "\n")
        return

    rag = await create_rag()
    try:
        package = await composer_search(rag, query, mode)
        report = format_search_results(package)
        print("\n" + "=" * 80)
        print(report)
        print("=" * 80 + "\n")
    finally:
        await _finalize_rag_safely(rag)

def _question_requests_sources(question: str) -> bool:
    """Detect whether the user explicitly asked for provenance links."""
    return bool(_SOURCE_REQUEST_RE.search(question))


def _extract_answer_reference_keys(answer: str) -> tuple[set[str], set[str]]:
    """Collect explicit citation keys and numbered reference bullets mentioned in the answer."""
    if not answer:
        return set(), set()

    reference_ids = {match.group(1).strip() for match in _REFERENCE_ID_RE.finditer(answer)}
    refs_section = answer.split("### References", 1)[1] if "### References" in answer else answer
    numbered_refs = {match.group(1) for match in _REFERENCE_BULLET_RE.finditer(refs_section)}
    reference_ids.update(
        token
        for token in _REFERENCE_TOKEN_RE.findall(answer)
        if not token.isdigit() and token.lower() != "reference_id"
    )
    return reference_ids, numbered_refs


def _load_adjacent_source_metadata(file_path: str) -> dict[str, Any]:
    """Read normalized sidecar metadata when the RAG metadata index is stale."""
    meta_path = config.resolve_project_path(file_path).with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_query_sources(query_result: dict[str, Any], limit: int = 5) -> list[dict[str, str]]:
    """Map LightRAG references back to Telegram post metadata."""
    if not isinstance(query_result, dict):
        return []

    data = query_result.get("data", {})
    references = data.get("references", []) if isinstance(data, dict) else []
    metadata_index = load_source_metadata_index()
    answer = str(query_result.get("llm_response", {}).get("content") or "")
    explicit_ids, numbered_refs = _extract_answer_reference_keys(answer)

    results = []
    seen_keys = set()
    filtered_references = []
    for idx, ref in enumerate(references, start=1):
        if not isinstance(ref, dict):
            continue
        ref_id = str(ref.get("reference_id") or "").strip()
        if explicit_ids or numbered_refs:
            if str(idx) in numbered_refs:
                filtered_references.append(ref)
                continue
            if ref_id and ref_id in explicit_ids:
                filtered_references.append(ref)
                continue
            continue
        filtered_references.append(ref)

    for ref in filtered_references:
        if not isinstance(ref, dict):
            continue
        file_path = ref.get("file_path")
        post_url_from_ref = str(ref.get("post_url") or ref.get("youtube_url") or "").strip()
        if not isinstance(file_path, str) or not file_path:
            if post_url_from_ref and post_url_from_ref not in seen_keys:
                seen_keys.add(post_url_from_ref)
                results.append(
                    {
                        "post_url": post_url_from_ref,
                        "channel": str(ref.get("channel") or "").strip(),
                        "date": str(ref.get("date") or "").strip(),
                        "file_path": "",
                    }
                )
                if len(results) >= limit:
                    break
            continue
        canonical_path = str(Path(file_path).resolve(strict=False))
        meta = metadata_index.get(canonical_path, {})
        if not meta:
            meta = _load_adjacent_source_metadata(canonical_path)
        source_path = str(meta.get("canonical_path") or canonical_path)
        post_url = str(meta.get("пост") or meta.get("post_url") or post_url_from_ref).strip()
        channel = str(meta.get("канал") or meta.get("channel_name") or ref.get("channel") or "").strip()
        date = str(meta.get("дата") or meta.get("date") or ref.get("date") or "").strip()
        key = post_url or source_path
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        results.append(
            {
                "post_url": post_url,
                "channel": channel,
                "date": date,
                "file_path": source_path,
            }
        )
        if len(results) >= limit:
            break
    return results


def _print_query_sources(query_result: dict[str, Any]) -> None:
    """Print a compact source block after the answer when requested."""
    sources = _extract_query_sources(query_result)
    print()
    print("Источники:")
    if not sources:
        print("  Не удалось поднять ссылки для этого ответа.")
        return

    for idx, source in enumerate(sources, start=1):
        label = source["post_url"] or source["file_path"]
        print(f"  {idx}. {label}")
        if source["channel"]:
            print(f"     Канал: {source['channel']}")
        if source["date"]:
            print(f"     Дата: {source['date']}")

question_requests_sources = _question_requests_sources
extract_query_sources = _extract_query_sources
print_query_sources = _print_query_sources
