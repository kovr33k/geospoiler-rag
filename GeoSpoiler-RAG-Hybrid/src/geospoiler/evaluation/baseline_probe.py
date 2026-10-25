"""
Baseline model probe for roadmap E1.

The probe records the query model/endpoint/config flags used for baseline work
and can run a small set of manual LightRAG queries. It does not modify .env,
clear caches, rebuild storage, or call any optional query integration.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.answering.query import query_rag_result
from geospoiler.compat.lightrag_loader import create_rag

RECOMMENDED_FLAGS = {
    "RERANKER_ENABLED": False,
    "HYBRID_SYNTH_ENABLED": False,
    "HYBRID_QUERY_CARDS_ENABLED": True,
}

BASELINE_MODEL_METADATA = "baseline_model_probe_metadata.json"
BASELINE_MODEL_REPORT = "baseline_model_probe_results.md"

BASELINE_QUERY_CASES = (
    {
        "id": "ultra_similarity",
        "question": "Что в базе говорится о сходстве ультралевых и ультраправых?",
        "profile": "answer",
    },
    {
        "id": "ultra_source",
        "question": "Откуда в базе тезис про ультралевых и ультраправых? Дай ссылку.",
        "profile": "source",
    },
    {
        "id": "trump_orban_support",
        "question": "Трамп реально поддерживал Орбана?",
        "profile": "source",
    },
    {
        "id": "cuba_talks",
        "question": "Что в базе говорится о Кубе и переговорах с США?",
        "profile": "answer",
    },
    {
        "id": "north_korea_russia",
        "question": "Что в базе говорится о северокорейских военных и России?",
        "profile": "answer",
    },
)


BASELINE_QUERY_TEXTS = (
    "Что в базе говорится о сходстве ультралевых и ультраправых?",
    "Откуда в базе тезис про ультралевых и ультраправых? Дай ссылку.",
    "Трамп реально поддерживал Орбана?",
    "Что в базе говорится о Кубе и переговорах с США?",
    "Что в базе говорится о северокорейских военных и России?",
)

BASELINE_QUERY_CASES = tuple(
    {**case, "question": question}
    for case, question in zip(BASELINE_QUERY_CASES, BASELINE_QUERY_TEXTS, strict=True)
)


@dataclass(frozen=True)
class BaselineProbeCaseResult:
    case_id: str
    question: str
    profile: str
    status: str
    duration_seconds: float
    answer_chars: int
    answer_preview: str
    reference_count: int
    fallback: str
    looks_corrupt: bool
    error: str = ""

    @property
    def stable(self) -> bool:
        return self.status == "ok" and self.answer_chars >= 20 and not self.looks_corrupt


@dataclass(frozen=True)
class BaselineProbeReport:
    checked_at: str
    mode: str
    query_model: str
    query_base_url: str
    query_profile_default: str
    config_flags: dict[str, bool]
    recommended_flags: dict[str, bool]
    results: list[BaselineProbeCaseResult]
    cache_buster: str = ""

    @property
    def stable_count(self) -> int:
        return sum(1 for result in self.results if result.stable)


def collect_baseline_metadata() -> dict[str, Any]:
    """Collect baseline model/config metadata without running network calls."""
    return {
        "created_at": _utc_now(),
        "query": {
            "model": config.QUERY_MODEL,
            "base_url": config.QUERY_BASE_URL,
            "timeout_seconds": config.QUERY_TIMEOUT_SECONDS,
        },
        "fallback_synthesis": {
            "model": config.FALLBACK_SYNTH_MODEL,
            "base_url": config.FALLBACK_SYNTH_BASE_URL,
            "enabled": config.HYBRID_SYNTH_ENABLED,
        },
        "candidate_models_to_check": [
            {
                "role": "query",
                "model": config.QUERY_MODEL,
                "base_url": config.QUERY_BASE_URL,
                "status": "current_config",
            },
            {
                "role": "synthesis",
                "model": config.FALLBACK_SYNTH_MODEL,
                "base_url": config.FALLBACK_SYNTH_BASE_URL,
                "status": "current_config",
            },
            {
                "role": "query_or_synthesis",
                "model": "minimaxai/minimax-m2.7",
                "base_url": config.QUERY_BASE_URL,
                "status": "roadmap_candidate",
            },
        ],
        "config_flags": _config_flags(),
        "recommended_flags": RECOMMENDED_FLAGS,
        "notes": [
            ".env is not modified by this probe.",
            "Use shell-level env overrides for E1 baseline runs.",
            "Clear only LightRAG LLM response cache when stale cross-model cache would invalidate E1.",
            "Do not run rebuild in E1.",
        ],
    }


async def run_baseline_probe(limit: int = 3, mode: str | None = None) -> BaselineProbeReport:
    """Run a small manual query probe and return structured results."""
    selected_cases = list(BASELINE_QUERY_CASES[: max(1, min(limit, len(BASELINE_QUERY_CASES)))])
    query_mode = mode or ("mix" if config.RERANKER_ENABLED else "hybrid")
    cache_buster = os.getenv("BASELINE_PROBE_CACHE_BUSTER", "").strip()
    results: list[BaselineProbeCaseResult] = []

    try:
        rag = await create_rag()
    except Exception as exc:
        error = f"create_rag failed before query run: {exc}"
        return BaselineProbeReport(
            checked_at=_utc_now(),
            mode=query_mode,
            query_model=config.QUERY_MODEL,
            query_base_url=config.QUERY_BASE_URL,
            query_profile_default="answer",
            config_flags=_config_flags(),
            recommended_flags=RECOMMENDED_FLAGS,
            cache_buster=cache_buster,
            results=[
                BaselineProbeCaseResult(
                    case_id=case["id"],
                    question=case["question"],
                    profile=case["profile"],
                    status="error",
                    duration_seconds=0.0,
                    answer_chars=0,
                    answer_preview="",
                    reference_count=0,
                    fallback="",
                    looks_corrupt=True,
                    error=error,
                )
                for case in selected_cases
            ],
        )

    try:
        for case_index, case in enumerate(selected_cases, start=1):
            started = time.perf_counter()
            try:
                query_result = await query_rag_result(
                    rag,
                    _with_cache_buster(case["question"], cache_buster, case_index),
                    mode=query_mode,
                    query_profile=case["profile"],
                )
                duration = round(time.perf_counter() - started, 3)
                answer = str(
                    query_result.get("llm_response", {}).get("content")
                    or query_result.get("response")
                    or ""
                ).strip()
                data = query_result.get("data", {}) if isinstance(query_result, dict) else {}
                references = data.get("references", []) if isinstance(data, dict) else []
                fallback = str(query_result.get("fallback") or query_result.get("hybrid_context") or "")
                results.append(
                    BaselineProbeCaseResult(
                        case_id=case["id"],
                        question=case["question"],
                        profile=case["profile"],
                        status="ok",
                        duration_seconds=duration,
                        answer_chars=len(answer),
                        answer_preview=_truncate(answer, 500),
                        reference_count=len(references) if isinstance(references, list) else 0,
                        fallback=fallback,
                        looks_corrupt=_answer_looks_corrupt(answer),
                    )
                )
            except Exception as exc:
                duration = round(time.perf_counter() - started, 3)
                results.append(
                    BaselineProbeCaseResult(
                        case_id=case["id"],
                        question=case["question"],
                        profile=case["profile"],
                        status="error",
                        duration_seconds=duration,
                        answer_chars=0,
                        answer_preview="",
                        reference_count=0,
                        fallback="",
                        looks_corrupt=True,
                        error=str(exc),
                    )
                )
    finally:
        finalize = getattr(rag, "finalize_storages", None)
        if finalize is not None:
            try:
                await asyncio.wait_for(finalize(), timeout=config.RAG_FINALIZE_TIMEOUT_SECONDS)
            except TimeoutError:
                pass

    return BaselineProbeReport(
        checked_at=_utc_now(),
        mode=query_mode,
        query_model=config.QUERY_MODEL,
        query_base_url=config.QUERY_BASE_URL,
        query_profile_default="answer",
        config_flags=_config_flags(),
        recommended_flags=RECOMMENDED_FLAGS,
        cache_buster=cache_buster,
        results=results,
    )


def write_baseline_metadata(
    metadata: dict[str, Any],
    artifacts_dir: Path | None = None,
) -> Path:
    artifacts_dir = artifacts_dir or (config.PROJECT_ROOT / "artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / BASELINE_MODEL_METADATA
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_probe_report(
    report: BaselineProbeReport,
    artifacts_dir: Path | None = None,
) -> tuple[Path, Path]:
    artifacts_dir = artifacts_dir or (config.PROJECT_ROOT / "artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = artifacts_dir / BASELINE_MODEL_METADATA
    report_path = artifacts_dir / BASELINE_MODEL_REPORT
    payload = {
        **collect_baseline_metadata(),
        "probe": _report_to_dict(report),
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(format_probe_report(report), encoding="utf-8")
    return metadata_path, report_path


def format_probe_report(report: BaselineProbeReport) -> str:
    lines = [
        "# Baseline Model Probe",
        "",
        f"Checked at: {report.checked_at}",
        f"Query model: `{report.query_model}`",
        f"Query base URL: `{report.query_base_url}`",
        f"Mode: `{report.mode}`",
        f"Cache buster: {'enabled' if report.cache_buster else 'disabled'}",
        "",
        "## Flags",
        "",
    ]
    for key, recommended in report.recommended_flags.items():
        actual = report.config_flags.get(key)
        marker = "OK" if actual == recommended else "DIFF"
        lines.append(f"- {key}: {actual} (recommended for E1: {recommended}) [{marker}]")

    lines.extend(
        [
            "",
            "## Manual Query Probe",
            "",
            f"Stable cases: {report.stable_count}/{len(report.results)}",
            "",
        ]
    )
    for idx, result in enumerate(report.results, start=1):
        lines.append(f"### {idx}. {result.case_id}")
        lines.append(f"- profile: `{result.profile}`")
        lines.append(f"- status: `{result.status}`")
        lines.append(f"- duration_seconds: {result.duration_seconds}")
        lines.append(f"- answer_chars: {result.answer_chars}")
        lines.append(f"- reference_count: {result.reference_count}")
        lines.append(f"- fallback: `{result.fallback}`")
        lines.append(f"- looks_corrupt: {result.looks_corrupt}")
        if result.error:
            lines.append(f"- error: {result.error}")
        if result.answer_preview:
            lines.append("")
            lines.append("Answer preview:")
            lines.append("")
            lines.append(result.answer_preview)
        lines.append("")
    return "\n".join(lines)


def _report_to_dict(report: BaselineProbeReport) -> dict[str, Any]:
    return {
        "checked_at": report.checked_at,
        "mode": report.mode,
        "query_model": report.query_model,
        "query_base_url": report.query_base_url,
        "query_profile_default": report.query_profile_default,
        "config_flags": report.config_flags,
        "recommended_flags": report.recommended_flags,
        "cache_buster": bool(report.cache_buster),
        "stable_count": report.stable_count,
        "total": len(report.results),
        "results": [asdict(result) | {"stable": result.stable} for result in report.results],
    }


def _config_flags() -> dict[str, bool]:
    return {
        "RERANKER_ENABLED": config.RERANKER_ENABLED,
        "HYBRID_SYNTH_ENABLED": config.HYBRID_SYNTH_ENABLED,
        "HYBRID_QUERY_CARDS_ENABLED": config.HYBRID_QUERY_CARDS_ENABLED,
    }


def _answer_looks_corrupt(answer: str) -> bool:
    if not answer.strip():
        return True
    lowered = answer.lower()
    bad_markers = [
        "degraded function cannot be invoked",
        "function cannot be invoked",
        "query failed",
        "traceback",
        "\ufffd",
        "benten",
        "picker all-france",
        "pump+",
    ]
    if any(marker in lowered for marker in bad_markers):
        return True
    mojibake_count = (
        answer.count("\u00c3\u0090")
        + answer.count("\u00c3\u0091")
        + answer.count("\u00d0")
        + answer.count("\u00d1")
    )
    if mojibake_count >= 6 and mojibake_count / max(1, len(answer)) > 0.03:
        return True
    cjk_count = sum(1 for char in answer if "\u4e00" <= char <= "\u9fff")
    return cjk_count >= 2


def _with_cache_buster(question: str, cache_buster: str, case_index: int) -> str:
    if not cache_buster:
        return question
    return f"{question}\u2063{cache_buster}:{case_index}"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
