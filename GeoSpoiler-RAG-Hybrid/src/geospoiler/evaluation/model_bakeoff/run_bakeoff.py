"""Run model bakeoff suites and write stable artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from geospoiler import config
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.evaluation.model_bakeoff.aggregate_report import write_reports
from geospoiler.evaluation.model_bakeoff.config_loader import ModelConfig, load_model_registry
from geospoiler.evaluation.model_bakeoff.prompts import case_user_prompt, messages_for_case
from geospoiler.evaluation.model_bakeoff.providers import (
    api_key_for,
    base_url_for,
    call_chat_completion,
    post_chat_completion,
    provider_options,
)
from geospoiler.evaluation.model_bakeoff.scoring import score_political_risk, score_quality

DEFAULT_MODELS_PATH = Path(__file__).with_name("models.yaml")
DEFAULT_SUITES_DIR = Path(__file__).with_name("suites")
DEFAULT_PROMPTS_DIR = Path(__file__).with_name("prompts")
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "model_bakeoff"


def estimate_cost_usd(model: ModelConfig, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost from per-million token registry prices."""
    cost = (
        (max(0, input_tokens) * model.input_price_per_m)
        + (max(0, output_tokens) * model.output_price_per_m)
    ) / 1_000_000
    return round(cost, 8)


def build_output_record(
    *,
    run_id: str,
    model: ModelConfig,
    role: str,
    suite: str,
    case_id: str,
    prompt_text: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    raw_response: str,
    parsed_response: Any,
    errors: list[str],
    retries: int,
    cache_buster: str,
) -> dict[str, Any]:
    """Build the canonical per-model output record."""
    return {
        "run_id": run_id,
        "timestamp": _utc_now(),
        "model_id": model.id,
        "provider": model.provider,
        "role": role,
        "suite": suite,
        "case_id": case_id,
        "prompt_hash": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "estimated_cost_usd": estimate_cost_usd(model, input_tokens, output_tokens),
        "raw_response": raw_response,
        "parsed_response": parsed_response,
        "errors": list(errors),
        "retries": retries,
        "cache_buster": cache_buster,
    }


def load_suite(path: Path) -> list[dict[str, Any]]:
    """Load JSONL suite cases."""
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def run_bakeoff(
    *,
    models_path: Path = DEFAULT_MODELS_PATH,
    suite_paths: list[Path],
    roles: set[str] | None = None,
    families: set[str] | None = None,
    model_ids: set[str] | None = None,
    limit: int | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    cache_buster: str = "",
) -> Path:
    """Run selected suites against selected models and return the run directory."""
    load_dotenv(config.PROJECT_ROOT / ".env")
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = DEFAULT_ARTIFACT_ROOT / run_id
    output_dir = run_dir / "model_outputs"
    scores_dir = run_dir / "scores"
    output_dir.mkdir(parents=True, exist_ok=True)
    scores_dir.mkdir(parents=True, exist_ok=True)

    models = _filter_models(load_model_registry(models_path), roles, families, model_ids)
    config_snapshot = {
        "run_id": run_id,
        "created_at": _utc_now(),
        "dry_run": dry_run,
        "roles": sorted(roles or []),
        "families": sorted(families or []),
        "model_ids": sorted(model_ids or []),
        "suites": [str(path) for path in suite_paths],
        "models": [asdict(model) for model in models],
        "flags": {
            "RERANKER_ENABLED": config.RERANKER_ENABLED,
            "HYBRID_SYNTH_ENABLED": config.HYBRID_SYNTH_ENABLED,
        },
    }
    (run_dir / "config_snapshot.json").write_text(
        json.dumps(config_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    score_records: list[dict[str, Any]] = []
    for suite_path in suite_paths:
        suite_cases = load_suite(suite_path)
        if limit is not None:
            suite_cases = suite_cases[:limit]
        for case in suite_cases:
            for model in models:
                role = _role_for_case(model, case)
                if role is None:
                    continue
                record = _run_case(
                    run_id=run_id,
                    model=model,
                    role=role,
                    suite=suite_path.stem,
                    case=case,
                    dry_run=dry_run,
                    cache_buster=cache_buster,
                )
                model_dir = output_dir / _safe_model_dir(model.id)
                model_dir.mkdir(parents=True, exist_ok=True)
                (model_dir / f"{case['id']}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                score_records.append(_score_record(record, model, case))

    _write_score_csvs(scores_dir, score_records)
    write_reports(run_dir, score_records)
    return run_dir


def _run_case(
    *,
    run_id: str,
    model: ModelConfig,
    role: str,
    suite: str,
    case: dict[str, Any],
    dry_run: bool,
    cache_buster: str,
) -> dict[str, Any]:
    messages, prompt_text = _messages_for_case(case)
    started = time.perf_counter()
    errors: list[str] = []
    retries = 0
    raw_response = ""
    parsed_response: Any = None
    input_tokens = _rough_tokens(prompt_text)
    output_tokens = 0

    if dry_run:
        raw_response = _dry_run_response(case)
        parsed_response = _parse_json(raw_response)
        output_tokens = _rough_tokens(raw_response)
    else:
        try:
            raw_response, usage = _call_chat_completion(model, messages, force_json=_case_requires_json(case))
            parsed_response = _parse_json(raw_response)
            input_tokens = int(usage.get("prompt_tokens") or input_tokens)
            output_tokens = int(usage.get("completion_tokens") or _rough_tokens(raw_response))
        except Exception as exc:
            errors.append(str(exc))

    latency_ms = int((time.perf_counter() - started) * 1000)
    return build_output_record(
        run_id=run_id,
        model=model,
        role=role,
        suite=suite,
        case_id=str(case["id"]),
        prompt_text=prompt_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        raw_response=raw_response,
        parsed_response=parsed_response,
        errors=errors,
        retries=retries,
        cache_buster=cache_buster,
    )


def _score_record(record: dict[str, Any], model: ModelConfig, case: dict[str, Any]) -> dict[str, Any]:
    scored = {
        "model_id": model.id,
        "provider": model.provider,
        "family": model.family,
        "role": record["role"],
        "suite": record["suite"],
        "case_id": record["case_id"],
        "estimated_cost_usd": record["estimated_cost_usd"],
        "latency_ms": record["latency_ms"],
    }
    if record.get("errors"):
        scored["quality"] = {
            "score": 0,
            "passed": False,
            "schema_valid": False,
            "missing": [],
            "forbidden": ["api_error"],
        }
        return scored
    if case.get("expected_behavior") or "political" in record["suite"] or case.get("suite") == "chinese_political_risk":
        scored["political_risk"] = asdict(score_political_risk(case, record))
    scored["quality"] = asdict(score_quality(case, record))
    return scored


def _write_score_csvs(scores_dir: Path, records: list[dict[str, Any]]) -> None:
    political_path = scores_dir / "political_risk_scores.csv"
    quality_path = scores_dir / "quality_scores.csv"
    with political_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["model_id", "role", "suite", "case_id", "risk_level", "score", "flags"])
        writer.writeheader()
        for record in records:
            risk = record.get("political_risk")
            if not risk:
                continue
            writer.writerow({
                "model_id": record["model_id"],
                "role": record["role"],
                "suite": record["suite"],
                "case_id": record["case_id"],
                "risk_level": risk["risk_level"],
                "score": risk["score"],
                "flags": ",".join(risk.get("flags") or []),
            })
    with quality_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["model_id", "role", "suite", "case_id", "passed", "score", "missing", "forbidden"])
        writer.writeheader()
        for record in records:
            quality = record.get("quality")
            if not quality:
                continue
            writer.writerow({
                "model_id": record["model_id"],
                "role": record["role"],
                "suite": record["suite"],
                "case_id": record["case_id"],
                "passed": quality["passed"],
                "score": quality["score"],
                "missing": ",".join(quality.get("missing") or []),
                "forbidden": ",".join(quality.get("forbidden") or []),
            })


def _filter_models(
    models: list[ModelConfig],
    roles: set[str] | None,
    families: set[str] | None,
    model_ids: set[str] | None,
) -> list[ModelConfig]:
    filtered = []
    for model in models:
        if families and model.family not in families:
            continue
        if model_ids and model.id not in model_ids:
            continue
        if roles and not roles.intersection(model.roles):
            continue
        filtered.append(model)
    return filtered


def _role_for_case(model: ModelConfig, case: dict[str, Any]) -> str | None:
    preferred = case.get("role")
    if preferred:
        return str(preferred)
    task_type = str(case.get("task_type", ""))
    role_map = {
        "translation_fidelity": "translation",
        "enrichment_json": "enrichment",
        "rag_build_extraction": "rag_build",
        "rag_build_tuple_extraction": "rag_build",
        "fixed_context_synthesis": "query",
        "fallback_synth": "fallback_synth",
        "source_preservation": "high_risk_enrichment",
        "direct_qa": "query",
        "script_pack": "script_pack",
    }
    role = role_map.get(task_type)
    if role:
        return role
    return model.roles[0] if model.roles else None


def _messages_for_case(case: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    return messages_for_case(case, prompts_dir=DEFAULT_PROMPTS_DIR)


def _case_user_prompt(case: dict[str, Any]) -> str:
    return case_user_prompt(case)


def _call_chat_completion(
    model: ModelConfig,
    messages: list[dict[str, str]],
    force_json: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    return call_chat_completion(
        model,
        messages,
        force_json=force_json,
        base_url_func=_base_url_for,
        api_key_func=_api_key_for,
        post_func=_post_chat_completion,
    )


def _post_chat_completion(base_url: str, api_key: str, payload: dict[str, Any]):
    return post_chat_completion(base_url, api_key, payload)


def _case_requires_json(case: dict[str, Any]) -> bool:
    return str(case.get("task_type", "")) in {"enrichment_json", "rag_build_extraction"}


def _provider_options(model: ModelConfig, base_url: str) -> dict[str, Any]:
    return provider_options(model, base_url)


def _base_url_for(model: ModelConfig) -> str:
    return base_url_for(model)


def _api_key_for(model: ModelConfig) -> str:
    return api_key_for(model)


def _dry_run_response(case: dict[str, Any]) -> str:
    expected = case.get("expected_behavior") or case.get("expected") or {}
    if case.get("task_type") == "enrichment_json":
        return json.dumps({
            "summary": "Dry run response.",
            "key_facts": [{"text": "Dry run fact.", "claim_type": "source_claim"}],
            "entities": {},
            "quotes": [],
            "expected": expected,
        }, ensure_ascii=False)
    return json.dumps({"dry_run": True, "expected": expected}, ensure_ascii=False)


def _parse_json(text: str) -> Any:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _rough_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _safe_model_dir(model_id: str) -> str:
    return model_id.replace("/", "__").replace(":", "_")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_csv_set(value: str) -> set[str] | None:
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run GeoSpoiler model bakeoff suites.")
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS_PATH)
    parser.add_argument("--suite", action="append", type=Path, required=True)
    parser.add_argument("--roles", default="")
    parser.add_argument("--families", default="")
    parser.add_argument("--model-ids", default="")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache-buster", default="")
    args = parser.parse_args(argv)

    run_dir = run_bakeoff(
        models_path=args.models,
        suite_paths=args.suite,
        roles=_parse_csv_set(args.roles),
        families=_parse_csv_set(args.families),
        model_ids=_parse_csv_set(args.model_ids),
        limit=args.limit,
        run_id=args.run_id,
        dry_run=args.dry_run,
        cache_buster=args.cache_buster,
    )
    print(f"Model bakeoff artifacts: {run_dir}")


if __name__ == "__main__":
    main()
