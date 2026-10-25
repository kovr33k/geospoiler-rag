"""
Focused live-LLM verification probe.

This is intentionally smaller than the full golden set. It targets the checks
that are currently queued in LLM_VERIFICATION_QUEUE.md: timeout behavior,
source grounding, and known Cuba/Narva failures.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.answering.query import query_rag_result
from geospoiler.cli.query import _extract_query_sources
from geospoiler.compat.lightrag_loader import create_rag

PROBE_RESULTS_FILE = Path("artifacts/llm_verification_probe_results.md")
PROBE_SCORES_FILE = Path("artifacts/llm_verification_probe_scores.json")

PROBE_CASES: list[dict[str, Any]] = [
    {
        "id": "f1_trump_orban_source",
        "question": "Трамп реально поддерживал Орбана? Дай источник.",
        "profile": "source",
        "must": ["трамп", "орбан"],
        "source_required": True,
        "source_any": ["3328128766/148", "3328128766/150", "3328128766/133"],
    },
    {
        "id": "q7_cuba_talks",
        "question": "Что в базе говорится о Кубе и переговорах с США?",
        "profile": "answer",
        "must": ["куб", "сша", "переговор"],
        "must_not": ["afd", "ультраправ", "нет информации", "отсутствует информация"],
        "source_any": ["Куба\\5.txt", "Куба\\8.txt", "3841808641/5", "3841808641/8"],
    },
    {
        "id": "q9_cuba_protests",
        "question": "Что в базе говорится о протестах на Кубе?",
        "profile": "answer",
        "must": ["куб", "протест", "электр"],
        "must_not": ["afd", "ультраправ", "нет информации", "отсутствует информация"],
        "source_any": ["Куба\\5.txt", "3841808641/5"],
    },
    {
        "id": "q22_narva_visuals",
        "question": "Какие кадры или визуалы можно использовать для ролика про Нарву и Эстонию?",
        "profile": "answer",
        "must": ["визуал", "кадр", "нарв"],
        "must_not": ["нельзя определить", "отсутствует информация"],
        "source_any": [
            "Балтийские страны\\2.txt",
            "Балтийские страны\\4.txt",
            "3889026624/2",
            "3889026624/4",
        ],
    },
    {
        "id": "north_korea_source_control",
        "question": "Откуда в базе тезис про северокорейских военных в России? Дай ссылку.",
        "profile": "source",
        "must": ["северокор", "росси"],
        "source_required": True,
    },
    {
        "id": "afd_funding_absence_control",
        "question": "Кто финансирует AfD?",
        "profile": "answer",
        "must": ["отсутств", "нельзя определить"],
        "must_not": ["россия финансирует", "кремль финансирует"],
    },
]


@dataclass(frozen=True)
class ProbeCaseResult:
    case_id: str
    question: str
    profile: str
    status: str
    duration_seconds: float
    score: int
    passed: bool
    answer_preview: str
    missing: list[str]
    forbidden: list[str]
    source_ok: bool
    source_any_ok: bool
    sources: list[dict[str, str]]
    error: str = ""


async def run_probe() -> dict[str, Any]:
    query_mode = _query_mode()
    results: list[ProbeCaseResult] = []
    cache_backup = _maybe_backup_lightrag_cache()
    rag = await create_rag()
    try:
        for index, case in enumerate(PROBE_CASES, start=1):
            started = time.perf_counter()
            print(f"Running probe {index}/{len(PROBE_CASES)}: {case['id']}", flush=True)
            try:
                query_result = await query_rag_result(
                    rag,
                    case["question"],
                    mode=query_mode,
                    query_profile=case["profile"],
                )
                duration = round(time.perf_counter() - started, 3)
                answer = str(query_result.get("llm_response", {}).get("content") or "").strip()
                sources = _extract_query_sources(query_result)
                score = _score_answer(answer, sources, case)
                results.append(ProbeCaseResult(
                    case_id=case["id"],
                    question=case["question"],
                    profile=case["profile"],
                    status="ok",
                    duration_seconds=duration,
                    answer_preview=_truncate(answer, 700),
                    sources=sources,
                    **score,
                ))
            except Exception as exc:
                duration = round(time.perf_counter() - started, 3)
                results.append(ProbeCaseResult(
                    case_id=case["id"],
                    question=case["question"],
                    profile=case["profile"],
                    status="error",
                    duration_seconds=duration,
                    score=0,
                    passed=False,
                    answer_preview="",
                    missing=list(case.get("must", [])),
                    forbidden=[],
                    source_ok=False,
                    source_any_ok=False,
                    sources=[],
                    error=str(exc),
                ))
    finally:
        try:
            await asyncio.wait_for(rag.finalize_storages(), timeout=config.RAG_FINALIZE_TIMEOUT_SECONDS)
        except TimeoutError:
            print(f"WARNING: finalize timed out after {config.RAG_FINALIZE_TIMEOUT_SECONDS}s", flush=True)

    summary = {
        "checked_at": _utc_now(),
        "query_model": config.QUERY_MODEL,
        "query_base_url": config.QUERY_BASE_URL,
        "fallback_synth_model": config.FALLBACK_SYNTH_MODEL,
        "mode": query_mode,
        "config_flags": {
            "RERANKER_ENABLED": config.RERANKER_ENABLED,
            "HYBRID_SYNTH_ENABLED": config.HYBRID_SYNTH_ENABLED,
            "HYBRID_QUERY_CARDS_ENABLED": config.HYBRID_QUERY_CARDS_ENABLED,
        },
        "cache_backup": str(cache_backup) if cache_backup else "",
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
    _write_json(summary, _scores_file())
    _write_markdown(summary, _results_file())
    return summary


def _score_answer(answer: str, sources: list[dict[str, str]], case: dict[str, Any]) -> dict[str, Any]:
    text = answer.casefold()
    missing = [term for term in case.get("must", []) if term.casefold() not in text]
    forbidden = [term for term in case.get("must_not", []) if term.casefold() in text]
    source_required = bool(case.get("source_required"))
    source_ok = not source_required or any(source.get("post_url") or source.get("file_path") for source in sources)
    source_any = case.get("source_any", [])
    source_blob = "\n".join(
        f"{source.get('post_url', '')}\n{source.get('file_path', '')}"
        for source in sources
    ).casefold()
    source_any_ok = not source_any or any(term.casefold() in source_blob for term in source_any)

    score = 100
    score -= 20 * len(missing)
    score -= 25 * len(forbidden)
    if not source_ok:
        score -= 30
    if not source_any_ok:
        score -= 30
    score = max(0, score)
    return {
        "score": score,
        "passed": score >= 80 and not missing and not forbidden and source_ok and source_any_ok,
        "missing": missing,
        "forbidden": forbidden,
        "source_ok": source_ok,
        "source_any_ok": source_any_ok,
    }


def _write_markdown(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# LLM Verification Probe",
        "",
        f"Checked at: {summary['checked_at']}",
        f"Query model: `{summary['query_model']}`",
        f"Fallback synthesis model: `{summary['fallback_synth_model']}`",
        f"Mode: `{summary['mode']}`",
        f"Cache backup: `{summary['cache_backup'] or 'not cleared'}`",
        (
            "Flags: "
            f"`RERANKER_ENABLED={summary['config_flags']['RERANKER_ENABLED']}`, "
            f"`HYBRID_SYNTH_ENABLED={summary['config_flags']['HYBRID_SYNTH_ENABLED']}`"
        ),
        "",
        (
            f"Summary: {summary['passed']}/{summary['total']} passed, "
            f"avg={summary['average_score']}, "
            f"avg_duration={summary['average_duration_seconds']}s"
        ),
        "",
        "| Case | Score | Pass | Seconds | Source | Missing | Forbidden |",
        "|---|---:|:---:|---:|:---:|---|---|",
    ]
    for case in summary["cases"]:
        lines.append(
            "| "
            f"{case['case_id']} | "
            f"{case['score']} | "
            f"{_yes_no(case['passed'])} | "
            f"{case['duration_seconds']} | "
            f"{_yes_no(case['source_ok'])}/{_yes_no(case['source_any_ok'])} | "
            f"{', '.join(case['missing']) or '-'} | "
            f"{', '.join(case['forbidden']) or '-'} |"
        )
    lines.append("")
    for case in summary["cases"]:
        lines.extend([
            f"## {case['case_id']}",
            "",
            f"Question: {case['question']}",
            "",
            case["answer_preview"] or f"ERROR: {case['error']}",
            "",
            "Sources:",
        ])
        if case["sources"]:
            for idx, source in enumerate(case["sources"], start=1):
                lines.append(f"- [{idx}] {source.get('post_url') or source.get('file_path') or 'unknown'}")
        else:
            lines.append("- none")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _query_mode() -> str:
    return "mix" if config.RERANKER_ENABLED else "hybrid"


def _results_file() -> Path:
    return Path(os.getenv("LLM_PROBE_RESULTS_FILE", str(PROBE_RESULTS_FILE)))


def _scores_file() -> Path:
    return Path(os.getenv("LLM_PROBE_SCORES_FILE", str(PROBE_SCORES_FILE)))


def _maybe_backup_lightrag_cache() -> Path | None:
    if os.getenv("LLM_PROBE_CLEAR_CACHE", "false").lower() != "true":
        return None
    cache_path = config.RAG_STORAGE_DIR / "kv_store_llm_response_cache.json"
    if not cache_path.exists():
        return None
    backup_dir = config.PROJECT_ROOT / "artifacts" / "llm_probe_cache_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"kv_store_llm_response_cache.{stamp}.json"
    shutil.move(str(cache_path), str(backup_path))
    return backup_path


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _yes_no(value: bool) -> str:
    return "Y" if value else "N"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    report = asyncio.run(run_probe())
    print(
        f"LLM probe: {report['passed']}/{report['total']} passed, "
        f"avg={report['average_score']}, "
        f"avg_duration={report['average_duration_seconds']}s"
    )
