"""Focused source-selection golden runner.

This checks whether user-visible query sources include the canonical evidence.
It intentionally stays separate from the full answer-quality golden set so
retrieval/source-grounding regressions are visible even when answer text passes.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.answering.query import query_rag_result
from geospoiler.cli.query import _extract_query_sources
from geospoiler.compat.lightrag_loader import create_rag
from tests.test_golden_set import (
    _env_float,
    _golden_query_mode,
    _golden_query_retries,
    _golden_retry_backoff_seconds,
    _is_retryable_query_error,
    _utc_now,
)

SOURCE_GOLDEN_RESULTS_FILE = Path("artifacts/source_selection_golden_results.md")
SOURCE_GOLDEN_SCORES_FILE = Path("artifacts/source_selection_golden_scores.json")


SOURCE_SELECTION_CASES: list[dict[str, Any]] = [
    {
        "id": "f1_trump_orban_source",
        "question": "Трамп реально поддерживал Орбана? Дай источник.",
        "profile": "source",
        "answer_must": ["трамп", "орбан"],
        "source_any": ["3328128766/148", "3328128766/150", "3328128766/133"],
        "max_rank": 3,
        "note": "Historical F1 source-grounding case: support for Orban should resolve to direct Hungary/Slovakia posts.",
    },
    {
        "id": "ultra_left_right_similarity_source",
        "question": "Что в базе говорится о сходстве ультралевых и ультраправых?",
        "profile": "answer",
        "answer_must": ["ультралев", "ультраправ"],
        "source_any": ["3299898370/11", "Ультра левые и ультра правые\\11.txt"],
        "max_rank": 1,
        "note": "The direct similarity claim is canonical in normalized source 11.",
    },
    {
        "id": "cuba_talks_source",
        "question": "Что в базе говорится о Кубе и переговорах с США?",
        "profile": "answer",
        "answer_must": ["куб", "сша", "переговор"],
        "source_any": ["3841808641/8", "3841808641/5", "Куба\\8.txt", "Куба\\5.txt"],
        "max_rank": 2,
        "note": "Cuba talks should be grounded in the direct Cuba posts, not adjacent US/Trump material.",
    },
    {
        "id": "q9_cuba_protests_source",
        "question": "Что в базе говорится о протестах на Кубе?",
        "profile": "answer",
        "answer_must": ["куб", "протест"],
        "source_any": ["3841808641/5", "Куба\\5.txt"],
        "max_rank": 1,
        "note": "Historical Q9 failure: protest answer must prioritize the direct Cuba protests post.",
    },
    {
        "id": "cuba_pressure_deal_source",
        "question": "Как база описывает отношение США к Кубе: давление или попытку сделки?",
        "profile": "answer",
        "answer_must": ["сша", "куб"],
        "source_any": ["3841808641/8", "Куба\\8.txt"],
        "max_rank": 1,
        "note": "Pressure-vs-deal wording should be grounded in the direct negotiation/pressure post.",
    },
    {
        "id": "narva_plans_source",
        "question": "Что в базе говорится о Нарве и планах России против Эстонии?",
        "profile": "answer",
        "answer_must": ["нарв", "эстон", "росси"],
        "source_any": ["3889026624/2", "3889026624/4", "Балтийские страны\\2.txt", "Балтийские страны\\4.txt"],
        "max_rank": 2,
        "note": "Narva planning question should ground in the direct Narva/Estonia posts.",
    },
    {
        "id": "q22_narva_visuals_top_source",
        "question": "Какие кадры или визуалы можно использовать для ролика про Нарву и Эстонию?",
        "profile": "answer",
        "answer_must": ["визуал", "кадр", "нарв"],
        "source_any": ["3889026624/2", "3889026624/4", "Балтийские страны\\2.txt", "Балтийские страны\\4.txt"],
        "max_rank": 2,
        "forbidden_top_any": ["3889026624/9", "3889026624/6", "Балтийские страны\\9.txt", "Балтийские страны\\6.txt"],
        "forbidden_top_n": 2,
        "note": "Historical Q22 weakness: broad Baltic visuals must not outrank direct Narva/Estonia visual sources.",
    },
    {
        "id": "afd_ukraine_stance_source",
        "question": "Что в базе говорится про отношение AfD к войне в Украине?",
        "profile": "answer",
        "answer_must": ["afd", "украин"],
        "source_any": ["3299898370/12", "3299898370/4", "Ультра левые и ультра правые\\12.txt", "Ультра левые и ультра правые\\4.txt"],
        "max_rank": 2,
        "note": "AfD/Ukraine stance should ground in the direct ultra-left/right topic posts.",
    },
    {
        "id": "afd_nepotism_source",
        "question": "Где в базе источник про кумовство в AfD? Дай ссылку.",
        "profile": "source",
        "answer_must": ["afd", "кумов"],
        "source_any": ["3299898370/13", "Ультра левые и ультра правые\\13.txt"],
        "max_rank": 2,
        "note": "Direct AfD nepotism query should resolve to the dedicated AfD nepotism post.",
    },
    {
        "id": "north_korea_troops_source",
        "question": "Откуда в базе тезис про северокорейских военных в России? Дай ссылку.",
        "profile": "source",
        "answer_must": ["северокор", "росси"],
        "source_any": ["3215620297/15", "3215620297/13", "Корея\\15.txt", "Корея\\13.txt"],
        "max_rank": 2,
        "note": "North Korea source control keeps the source profile honest on a stable topic.",
    },
]


@dataclass(frozen=True)
class SourceCaseResult:
    case_id: str
    question: str
    profile: str
    status: str
    duration_seconds: float
    score: int
    passed: bool
    answer_preview: str
    answer_missing: list[str]
    source_any_ok: bool
    source_rank: int | None
    rank_ok: bool
    forbidden_top_hits: list[str]
    sources: list[dict[str, str]]
    note: str
    error: str = ""


async def run_source_selection_golden() -> dict[str, Any]:
    query_mode = _golden_query_mode()
    cases = _source_cases_for_run()
    results: list[SourceCaseResult] = []
    rag = await create_rag()
    try:
        for index, case in enumerate(cases, start=1):
            started = time.perf_counter()
            print(f"Running source case {index}/{len(cases)}: {case['id']}", flush=True)
            try:
                query_result = await _query_rag_result_with_retries(
                    rag,
                    case["question"],
                    mode=query_mode,
                    query_profile=case["profile"],
                )
                duration = round(time.perf_counter() - started, 3)
                answer = str(query_result.get("llm_response", {}).get("content") or "").strip()
                sources = _extract_query_sources(query_result, limit=_source_limit())
                score = _score_source_selection(answer, sources, case)
                results.append(
                    SourceCaseResult(
                        case_id=case["id"],
                        question=case["question"],
                        profile=case["profile"],
                        status="ok",
                        duration_seconds=duration,
                        answer_preview=_truncate(answer, 700),
                        sources=sources,
                        note=str(case.get("note") or ""),
                        error="",
                        **score,
                    )
                )
            except Exception as exc:
                duration = round(time.perf_counter() - started, 3)
                results.append(
                    SourceCaseResult(
                        case_id=case["id"],
                        question=case["question"],
                        profile=case["profile"],
                        status="error",
                        duration_seconds=duration,
                        score=0,
                        passed=False,
                        answer_preview="",
                        answer_missing=list(case.get("answer_must", [])),
                        source_any_ok=False,
                        source_rank=None,
                        rank_ok=False,
                        forbidden_top_hits=[],
                        sources=[],
                        note=str(case.get("note") or ""),
                        error=str(exc),
                    )
                )
            if index < len(cases):
                await asyncio.sleep(_source_query_delay_seconds())
    finally:
        try:
            await asyncio.wait_for(rag.finalize_storages(), timeout=config.RAG_FINALIZE_TIMEOUT_SECONDS)
        except TimeoutError:
            print(f"WARNING: finalize timed out after {config.RAG_FINALIZE_TIMEOUT_SECONDS}s", flush=True)

    summary = {
        "checked_at": _utc_now(),
        "query_model": config.QUERY_MODEL,
        "query_base_url": config.QUERY_BASE_URL,
        "mode": query_mode,
        "config_flags": {
            "RERANKER_ENABLED": config.RERANKER_ENABLED,
            "HYBRID_SYNTH_ENABLED": config.HYBRID_SYNTH_ENABLED,
            "HYBRID_QUERY_CARDS_ENABLED": config.HYBRID_QUERY_CARDS_ENABLED,
        },
        "source_limit": _source_limit(),
        "total": len(results),
        "passed": sum(1 for item in results if item.passed),
        "failed": sum(1 for item in results if not item.passed),
        "average_score": round(sum(item.score for item in results) / max(1, len(results)), 1),
        "average_duration_seconds": round(
            sum(item.duration_seconds for item in results) / max(1, len(results)),
            3,
        ),
        "cases": [item.__dict__ for item in results],
    }
    _write_json(summary, _source_scores_file())
    _write_markdown(summary, _source_results_file())
    print(
        f"Source-selection golden: {summary['passed']}/{summary['total']} passed, "
        f"avg={summary['average_score']}"
    )
    return summary


def _score_source_selection(answer: str, sources: list[dict[str, str]], case: dict[str, Any]) -> dict[str, Any]:
    answer_text = answer.casefold()
    answer_missing = [term for term in case.get("answer_must", []) if term.casefold() not in answer_text]
    source_any = list(case.get("source_any", []))
    source_rank = _first_matching_source_rank(sources, source_any)
    source_any_ok = not source_any or source_rank is not None

    max_rank = case.get("max_rank")
    rank_ok = source_rank is not None and (not max_rank or source_rank <= int(max_rank))
    if not source_any:
        rank_ok = True

    forbidden_top_hits = _forbidden_top_hits(
        sources,
        list(case.get("forbidden_top_any", [])),
        int(case.get("forbidden_top_n") or 0),
    )

    score = 100
    score -= 10 * len(answer_missing)
    if not source_any_ok:
        score -= 50
    elif not rank_ok:
        score -= 20
    score -= 25 * len(forbidden_top_hits)
    score = max(0, score)

    return {
        "score": score,
        "passed": score >= 80 and not answer_missing and source_any_ok and rank_ok and not forbidden_top_hits,
        "answer_missing": answer_missing,
        "source_any_ok": source_any_ok,
        "source_rank": source_rank,
        "rank_ok": rank_ok,
        "forbidden_top_hits": forbidden_top_hits,
    }


def _first_matching_source_rank(sources: list[dict[str, str]], terms: list[str]) -> int | None:
    if not terms:
        return None
    for rank, source in enumerate(sources, start=1):
        blob = _source_blob(source)
        if any(_term_matches_source_blob(term, blob) for term in terms):
            return rank
    return None


def _forbidden_top_hits(sources: list[dict[str, str]], terms: list[str], top_n: int) -> list[str]:
    if not terms or top_n <= 0:
        return []
    hits: list[str] = []
    for source in sources[:top_n]:
        blob = _source_blob(source)
        for term in terms:
            if _term_matches_source_blob(term, blob) and term not in hits:
                hits.append(term)
    return hits


def _term_matches_source_blob(term: str, blob: str) -> bool:
    normalized = term.casefold().strip()
    variants = {
        normalized,
        normalized.replace("\\", "/"),
        normalized.replace("/", "\\"),
    }
    return any(variant and variant in blob for variant in variants)


def _source_blob(source: dict[str, str]) -> str:
    raw = "\n".join(
        str(source.get(key) or "")
        for key in ("post_url", "file_path", "channel", "date")
    ).casefold()
    return "\n".join({raw, raw.replace("\\", "/"), raw.replace("/", "\\")})


async def _query_rag_result_with_retries(
    rag: Any,
    question: str,
    mode: str,
    query_profile: str,
) -> dict[str, Any]:
    retries = _golden_query_retries()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await query_rag_result(rag, question, mode=mode, query_profile=query_profile)
        except Exception as exc:
            last_error = exc
            if attempt >= retries or not _is_retryable_query_error(exc):
                raise
            delay = _golden_retry_backoff_seconds() * (attempt + 1)
            print(f"Retrying source-selection query after {delay:g}s due to transient error: {exc}", flush=True)
            await asyncio.sleep(delay)
    raise RuntimeError(f"Source-selection query failed without result: {last_error}")


def _source_cases_for_run() -> list[dict[str, Any]]:
    selected_ids = _selected_case_ids()
    cases = SOURCE_SELECTION_CASES
    if selected_ids:
        selected = set(selected_ids)
        cases = [case for case in cases if case["id"] in selected]
    limit = _source_case_limit()
    if limit is not None:
        cases = cases[:limit]
    return cases


def _selected_case_ids() -> list[str]:
    raw = os.getenv("SOURCE_GOLDEN_CASE_IDS", "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _source_case_limit() -> int | None:
    raw = os.getenv("SOURCE_GOLDEN_CASE_LIMIT", "").strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError:
        return None
    return limit if limit > 0 else None


def _source_limit() -> int:
    return max(1, int(_env_float("SOURCE_GOLDEN_SOURCE_LIMIT", 8)))


def _source_query_delay_seconds() -> float:
    if os.getenv("SOURCE_GOLDEN_QUERY_DELAY_SECONDS", "").strip():
        return _env_float("SOURCE_GOLDEN_QUERY_DELAY_SECONDS", 0.0)
    return _env_float("GOLDEN_QUERY_DELAY_SECONDS", 0.0)


def _source_results_file() -> Path:
    return Path(os.getenv("SOURCE_GOLDEN_RESULTS_FILE", str(SOURCE_GOLDEN_RESULTS_FILE)))


def _source_scores_file() -> Path:
    return Path(os.getenv("SOURCE_GOLDEN_SCORES_FILE", str(SOURCE_GOLDEN_SCORES_FILE)))


def _write_json(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Source-Selection Golden Results",
        "",
        f"Checked at: {summary['checked_at']}",
        f"Query model: `{summary['query_model']}`",
        f"Query base URL: `{summary['query_base_url']}`",
        f"Mode: `{summary['mode']}`",
        f"Source limit: `{summary['source_limit']}`",
        (
            "Flags: "
            f"`RERANKER_ENABLED={summary['config_flags']['RERANKER_ENABLED']}`, "
            f"`HYBRID_SYNTH_ENABLED={summary['config_flags']['HYBRID_SYNTH_ENABLED']}`, "
            f"`HYBRID_QUERY_CARDS_ENABLED={summary['config_flags']['HYBRID_QUERY_CARDS_ENABLED']}`"
        ),
        "",
        (
            f"Summary: {summary['passed']}/{summary['total']} passed, "
            f"avg={summary['average_score']}, "
            f"avg_duration={summary['average_duration_seconds']}s"
        ),
        "",
        "| Case | Score | Pass | Rank | Missing | Top-forbidden |",
        "|---|---:|:---:|---:|---|---|",
    ]
    for case in summary["cases"]:
        lines.append(
            "| "
            f"{case['case_id']} | "
            f"{case['score']} | "
            f"{_yes_no(case['passed'])} | "
            f"{case['source_rank'] or '-'} | "
            f"{', '.join(case['answer_missing']) or '-'} | "
            f"{', '.join(case['forbidden_top_hits']) or '-'} |"
        )
    lines.append("")
    for case in summary["cases"]:
        lines.extend(
            [
                f"## {case['case_id']}",
                "",
                f"Question: {case['question']}",
                "",
                f"Profile: `{case['profile']}`",
                "",
                f"Note: {case['note'] or '-'}",
                "",
                case["answer_preview"] or f"ERROR: {case['error']}",
                "",
                "Sources:",
            ]
        )
        if case["sources"]:
            for index, source in enumerate(case["sources"], start=1):
                lines.append(
                    f"- {index}. {source.get('post_url') or '-'} | {source.get('file_path') or '-'}"
                )
        else:
            lines.append("- none")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _yes_no(value: bool) -> str:
    return "Y" if value else "N"


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(run_source_selection_golden())
