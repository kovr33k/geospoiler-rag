"""Aggregate model bakeoff scores into role recommendations."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROLE_TO_ENV = {
    "rag_build": "RAG_BUILD_MODEL",
    "enrichment": "ENRICHMENT_MODEL",
    "high_risk_enrichment": "HIGH_RISK_ENRICHMENT_MODEL",
    "translation": "TRANSLATION_MODEL",
    "translation_smoke": "TRANSLATION_MODEL",
    "query": "QUERY_MODEL",
    "query_lite": "QUERY_MODEL",
    "fallback_synth": "FALLBACK_SYNTH_MODEL",
    "script_pack": "SCRIPT_PACK_MODEL",
    "classification": "CLASSIFICATION_MODEL",
}


def summarize_score_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact recommendations and risk buckets from scored records."""
    red_models = sorted({
        record["model_id"]
        for record in records
        if _risk_level(record) == "RED"
    })
    yellow_models = sorted({
        record["model_id"]
        for record in records
        if _risk_level(record) == "YELLOW"
    })

    by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("model_id") in red_models:
            continue
        by_role[str(record.get("role", ""))].append(record)

    recommended: dict[str, tuple[str, tuple[float, float, float, float]]] = {}
    for role, role_records in by_role.items():
        env_name = _ROLE_TO_ENV.get(role)
        if not env_name:
            continue
        by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in role_records:
            by_model[str(record["model_id"])].append(record)
        best_model, best_score = max(
            (
                (model_id, _recommendation_group_score(model_records))
                for model_id, model_records in by_model.items()
            ),
            key=lambda item: item[1],
        )
        current = recommended.get(env_name)
        if current is None or best_score > current[1]:
            recommended[env_name] = (best_model, best_score)

    return {
        "recommended": {env_name: model_id for env_name, (model_id, _score) in recommended.items()},
        "do_not_use_for_high_risk": red_models,
        "yellow_models": yellow_models,
        "red_models": red_models,
    }


def write_reports(run_dir: Path, score_records: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    """Write role recommendations, report.md, and failures.md."""
    summary = summarize_score_records(score_records)
    scores_dir = run_dir / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)
    recommendations_path = scores_dir / "role_recommendations.json"
    report_path = run_dir / "report.md"
    failures_path = run_dir / "failures.md"

    recommendations_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(_format_report(summary, score_records), encoding="utf-8")
    failures_path.write_text(_format_failures(score_records), encoding="utf-8")
    return recommendations_path, report_path, failures_path


def _format_report(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        "# Model Bakeoff Report",
        "",
        "## Recommendations",
        "",
    ]
    if summary["recommended"]:
        for role, model_id in sorted(summary["recommended"].items()):
            lines.append(f"- `{role}`: `{model_id}`")
    else:
        lines.append("- No recommendations from this run.")
    lines.extend([
        "",
        "## Risk Buckets",
        "",
        f"- RED: {', '.join(summary['red_models']) or '-'}",
        f"- YELLOW: {', '.join(summary['yellow_models']) or '-'}",
        "",
        "## Records",
        "",
        "| Model | Role | Risk | Quality |",
        "|---|---|---:|---:|",
    ])
    for record in records:
        risk = record.get("political_risk", {}).get("risk_level", "")
        q_score = record.get("quality", {}).get("score", "")
        lines.append(f"| `{record.get('model_id', '')}` | `{record.get('role', '')}` | {risk or '-'} | {q_score or '-'} |")
    lines.append("")
    return "\n".join(lines)


def _format_failures(records: list[dict[str, Any]]) -> str:
    lines = ["# Model Bakeoff Failures", ""]
    failures = [record for record in records if _risk_level(record) == "RED" or not record.get("quality", {}).get("passed", True)]
    if not failures:
        lines.append("No deterministic failures recorded.")
        lines.append("")
        return "\n".join(lines)
    for record in failures:
        lines.append(f"## {record.get('model_id')} / {record.get('case_id', '')}")
        lines.append("")
        risk = record.get("political_risk") or {}
        quality = record.get("quality") or {}
        if risk:
            lines.append(f"- risk_level: {risk.get('risk_level')}")
            lines.append(f"- flags: {', '.join(risk.get('flags') or []) or '-'}")
        if quality:
            lines.append(f"- quality_score: {quality.get('score')}")
            lines.append(f"- missing: {', '.join(quality.get('missing') or []) or '-'}")
            lines.append(f"- forbidden: {', '.join(quality.get('forbidden') or []) or '-'}")
        lines.append("")
    return "\n".join(lines)


def _risk_level(record: dict[str, Any]) -> str:
    risk = record.get("political_risk")
    if not isinstance(risk, dict):
        return ""
    return str(risk.get("risk_level", ""))


def _recommendation_score(record: dict[str, Any]) -> float:
    if not record:
        return -1
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    risk = record.get("political_risk") if isinstance(record.get("political_risk"), dict) else {}
    score = float(quality.get("score") or risk.get("score") or 0)
    if risk.get("risk_level") == "YELLOW":
        score -= 15
    return score


def _recommendation_group_score(records: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    if not records:
        return (-1, -1, 0, 0)
    scores = [_recommendation_score(record) for record in records]
    quality = [record.get("quality") for record in records if isinstance(record.get("quality"), dict)]
    passed = sum(1 for item in quality if item.get("passed", False))
    pass_rate = passed / len(quality) if quality else 0.0
    avg_cost = sum(float(record.get("estimated_cost_usd") or 0) for record in records) / len(records)
    avg_latency = sum(float(record.get("latency_ms") or 0) for record in records) / len(records)
    return (
        sum(scores) / len(scores),
        pass_rate,
        -avg_cost,
        -avg_latency,
    )
