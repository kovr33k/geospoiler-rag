"""Resumable, identity-checked A/B harness for the Late-Fusion RAG V1 contract."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import locale
import os
import platform
import random
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.answering.query import query_rag_result  # noqa: E402
from geospoiler.cli.runtime import _finalize_rag_safely  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.providers import (
    llm_backend,  # noqa: E402
    reranker,  # noqa: E402
)
from geospoiler.retrieval.lightrag.factory import create_rag  # noqa: E402

FROZEN_CASES = (
    {
        "id": "LF01",
        "profile": "answer",
        "question": "Что в базе говорится о сходстве ультралевых и ультраправых?",
    },
    {
        "id": "LF02",
        "profile": "source",
        "question": "Трамп реально поддерживал Орбана? Дай ссылки на конкретные материалы.",
    },
    {
        "id": "LF03",
        "profile": "answer",
        "question": "Как база описывает отношение США к Кубе: давление или попытку сделки?",
    },
    {
        "id": "LF04",
        "profile": "source",
        "question": "Что известно о поставках нефти на Кубу и позиции Трампа? Укажи конкретные числа, даты и источники.",
    },
    {
        "id": "LF05",
        "profile": "answer",
        "question": "Что в базе говорится о Нарве и планах России против Эстонии?",
    },
    {
        "id": "LF06",
        "profile": "source",
        "question": "Что в длинном видео говорится о проекте «Восточный щит», роли Starlink и применимости российского боевого опыта против НАТО? Собери несколько разных тезисов и дай таймкоды.",
    },
    {
        "id": "LF07",
        "profile": "answer",
        "question": "Как связаны северокорейские военные, экспорт в Россию и уровень жизни в КНДР? Как в материале описана роль Китая?",
    },
    {
        "id": "LF08",
        "profile": "answer",
        "question": "Что в базе говорится о Британии, Стармере и оборонном сотрудничестве с ЕС?",
    },
    {
        "id": "LF09",
        "profile": "answer",
        "question": "Что в базе говорится о риске утечки информации от AfD к России?",
    },
    {
        "id": "LF10",
        "profile": "answer",
        "question": "Кто финансирует AfD?",
    },
)
CRITERIA = ("completeness", "specificity", "relevance", "no_unsupported", "citations")
REQUIRED_AUTOMATIC_GATES = frozenset(
    {
        "late_fusion_tests",
        "affected_regression_tests",
        "full_suite_flag_false",
        "full_suite_flag_true",
        "ruff",
        "compile_import",
        "git_diff_check",
    }
)
SCOPED_PATHS = (
    ".env.example",
    "ARCHITECTURE.md",
    "LATE_FUSION_RAG_CLOSEOUT_TZ.md",
    "LATE_FUSION_RAG_TZ.md",
    "README.md",
    "cli_query.py",
    "config.py",
    "loader/late_fusion.py",
    "loader/query.py",
    "requirements.txt",
    "reranker.py",
    "retrieval/card_fts.py",
    "retrieval/source_registry.py",
    "scripts/late_fusion_ab.py",
    "tests/test_late_fusion.py",
    "tests/test_late_fusion_ab.py",
    "tests/test_card_fts.py",
    "tests/test_loader.py",
    "tests/test_reranker.py",
)


def build_identity(
    *,
    seed: str = "late-fusion-v1",
    started_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Capture every input that may make an A/B result non-comparable."""
    implementation_manifest = _implementation_manifest()
    return {
        "run_id": run_id or f"late-fusion-{uuid.uuid4()}",
        "started_at": started_at or _utc_now(),
        "run_seed": seed,
        "blind_mapping_sha256": _sha256_text(_canonical_json(_blind_mapping(seed))),
        "git_commit": _git_output("rev-parse", "HEAD"),
        "dirty_tree_manifest": _git_output("status", "--short", "--", *SCOPED_PATHS),
        "implementation_scoped_diff_hash": _sha256_text(
            _git_output("diff", "--binary", "--", *SCOPED_PATHS)
        ),
        "implementation_manifest": implementation_manifest,
        "implementation_manifest_sha256": _sha256_text(_canonical_json(implementation_manifest)),
        "spec_sha256": _sha256_path(PROJECT_ROOT / "LATE_FUSION_RAG_TZ.md"),
        "closeout_spec_sha256": _sha256_path(PROJECT_ROOT / "LATE_FUSION_RAG_CLOSEOUT_TZ.md"),
        "frozen_query_set_sha256": _sha256_text(_canonical_json(FROZEN_CASES)),
        "lightrag_version": _package_version("lightrag-hku"),
        "llm_profile": config.LLM_PROFILE,
        "query_model": llm_backend.active_model_for("query"),
        "fallback_synth_model": llm_backend.active_model_for("fallback_synth"),
        "query_mode": "mix",
        "late_fusion_config": {
            "card_top_k": config.LATE_FUSION_CARD_TOP_K,
            "youtube_top_k": config.LATE_FUSION_YOUTUBE_TOP_K,
            "max_sources": config.LATE_FUSION_MAX_SOURCES,
            "max_input_tokens": config.LATE_FUSION_MAX_INPUT_TOKENS,
            "output_token_reserve": config.LATE_FUSION_OUTPUT_TOKEN_RESERVE,
            "runtime_context_limit": config.LATE_FUSION_RUNTIME_CONTEXT_LIMIT,
            "fts_timeout_seconds": config.LATE_FUSION_FTS_TIMEOUT_SECONDS,
            "query_timeout_seconds": config.QUERY_TIMEOUT_SECONDS,
            "llm_timeout_seconds": config.CODEX_LLM_TIMEOUT_SECONDS,
            "reranker_enabled": config.RERANKER_ENABLED,
            "reranker_provider": config.RERANKER_PROVIDER,
            "reranker_model": config.RERANKER_MODEL,
            "reranker_base_url": config.RERANKER_BASE_URL,
            "reranker_top_n": config.RERANKER_TOP_N,
            "reranker_candidate_pool": config.RERANKER_CANDIDATE_POOL,
            "wiki_enabled": config.WIKI_ENABLED,
            "hybrid_query_wiki_enabled": config.HYBRID_QUERY_WIKI_ENABLED,
        },
        "source_registry_sha256": _sha256_path(config.SOURCE_REGISTRY_DB_PATH),
        "card_fts_sha256": _sha256_path(config.CARD_FTS_DB_PATH),
        "rag_storage_manifest_sha256": _directory_manifest_hash(
            config.RAG_STORAGE_DIR,
            ignored_relative_paths={"kv_store_llm_response_cache.json"},
        ),
        "enriched_corpus_manifest_sha256": _directory_manifest_hash(config.ENRICHED_DIR),
        "tokenizer_identity": "lightrag.utils.TiktokenTokenizer:gpt-4o-mini",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "preferred_encoding": locale.getpreferredencoding(False),
            "filesystem_encoding": sys.getfilesystemencoding(),
        },
    }


def prepare_run(output_dir: Path, *, resume: bool, seed: str) -> dict[str, Any]:
    """Create or validate a run directory before any model request is sent."""
    output_dir.mkdir(parents=True, exist_ok=True)
    identity_path = output_dir / "identity.json"
    if identity_path.exists():
        existing = _read_json(identity_path)
        identity = build_identity(
            seed=seed,
            started_at=str(existing.get("started_at") or ""),
            run_id=str(existing.get("run_id") or ""),
        )
        if existing != identity:
            raise RuntimeError("A/B run identity changed; use a new output directory instead of resuming.")
        if not resume:
            raise RuntimeError("Run directory already exists; pass --resume after reviewing its identity.")
    elif resume:
        raise RuntimeError("Cannot resume: identity.json is missing.")
    else:
        identity = build_identity(seed=seed)
        _write_json(identity_path, identity)
        _write_json(output_dir / "reviews.json", _review_template())
        _write_json(output_dir / "blind_mapping.json", _blind_mapping(seed))
    return identity


async def run_cases(output_dir: Path, *, resume: bool, seed: str) -> dict[str, Any]:
    identity = prepare_run(output_dir, resume=resume, seed=seed)
    state_path = output_dir / "run_state.json"
    state = (
        _read_json(state_path)
        if state_path.exists()
        else {
            "run_id": identity.get("run_id"),
            "run_seed": seed,
            "completed": [],
            "case_sha256": {},
            "started_at": identity.get("started_at"),
        }
    )
    if state.get("run_id") != identity.get("run_id") or state.get("run_seed") != seed:
        raise RuntimeError("Run state identity/seed mismatch; start a new run directory.")
    completed = set(state.get("completed") or [])
    _validate_completed_case_hashes(output_dir, state)

    rag = await create_rag()
    try:
        for case in FROZEN_CASES:
            _assert_current_identity(output_dir)
            case_id = case["id"]
            case_path = output_dir / "cases" / f"{case_id}.json"
            if case_id in completed and case_path.exists():
                continue
            artifact = await _run_case(rag, case)
            _write_json(case_path, artifact)
            completed.add(case_id)
            state.setdefault("case_sha256", {})[case_id] = _sha256_path(case_path)
            state["completed"] = [item["id"] for item in FROZEN_CASES if item["id"] in completed]
            state["updated_at"] = _utc_now()
            _write_json(state_path, state)
            _write_blind_case(output_dir, case_id, artifact, mapping=_read_json(output_dir / "blind_mapping.json"))
    finally:
        await _finalize_rag_safely(rag)

    _assert_current_identity(output_dir)
    _validate_completed_case_hashes(output_dir, state)
    report = {
        "identity": identity,
        "completed": state["completed"],
        "complete": len(state["completed"]) == len(FROZEN_CASES),
        "generated_at": _utc_now(),
    }
    _write_json(output_dir / "run_summary.json", report)
    write_blind_review_packet(output_dir)
    return report


async def _run_case(rag: Any, case: dict[str, str]) -> dict[str, Any]:
    question = case["question"]
    profile = case["profile"]
    original = config.LATE_FUSION_ENABLED
    try:
        config.LATE_FUSION_ENABLED = False
        reranker.reset_reranker_stats()
        legacy = await query_rag_result(rag, question, mode="mix", query_profile=profile)
        legacy_reranker = reranker.get_reranker_stats()
        config.LATE_FUSION_ENABLED = True
        reranker.reset_reranker_stats()
        late_fusion = await query_rag_result(rag, question, mode="mix", query_profile=profile)
        late_fusion_reranker = reranker.get_reranker_stats()
    finally:
        config.LATE_FUSION_ENABLED = original
    return {
        "id": case["id"],
        "question": question,
        "profile": profile,
        "legacy": {**_result_artifact(legacy), "reranker": legacy_reranker},
        "late_fusion": {**_result_artifact(late_fusion), "reranker": late_fusion_reranker},
        "completed_at": _utc_now(),
    }


def score_reviews(output_dir: Path) -> dict[str, Any]:
    """Fail closed unless the immutable run and every automatic/manual gate pass."""
    identity = _read_json(output_dir / "identity.json")
    reviews = _read_json(output_dir / "reviews.json")
    mapping = _read_json(output_dir / "blind_mapping.json")
    validation_errors = _validate_acceptance_run(output_dir, identity=identity, reviews=reviews, mapping=mapping)
    review_items = [item for item in reviews.get("cases", []) if isinstance(item, dict)]
    by_case = {str(item.get("id")): item for item in review_items}
    summaries: list[dict[str, Any]] = []
    for case in FROZEN_CASES:
        case_id = case["id"]
        case_path = output_dir / "cases" / f"{case_id}.json"
        if not case_path.exists():
            continue
        artifact = _read_json(case_path)
        review = by_case.get(case_id)
        if review is None:
            continue
        try:
            scores = {
                criterion: _score_blind_choice(review.get(criterion), mapping[case_id], case_id, criterion)
                for criterion in CRITERIA
            }
        except (KeyError, ValueError) as exc:
            validation_errors.append(str(exc))
            continue
        late_variant = artifact.get("late_fusion") if isinstance(artifact.get("late_fusion"), dict) else {}
        pipeline = str(late_variant.get("late_fusion", {}).get("pipeline") or "")
        fallback = bool(late_variant.get("fallback")) or pipeline != "late_fusion"
        total = sum(scores.values())
        lf10_fail = case_id == "LF10" and review.get("unsupported_financing_claim") is not False
        non_worse = total >= 0 and scores["citations"] != -1 and not fallback and not lf10_fail
        materially_better = (
            total >= 2
            and (scores["completeness"] == 1 or scores["specificity"] == 1)
            and all(score != -1 for score in scores.values())
            and not fallback
            and not lf10_fail
        )
        summaries.append(
            {
                "id": case_id,
                "scores": scores,
                "total": total,
                "late_fusion_fallback": fallback,
                "pipeline": pipeline,
                "lf10_automatic_fail": lf10_fail,
                "non_worse": non_worse,
                "materially_better": materially_better,
            }
        )
    accepted = (
        not validation_errors
        and len(summaries) == len(FROZEN_CASES)
        and sum(item["non_worse"] for item in summaries) >= 9
        and sum(item["materially_better"] for item in summaries) >= 5
        and all(not item["late_fusion_fallback"] for item in summaries)
        and all(not item["lf10_automatic_fail"] for item in summaries)
    )
    report: dict[str, Any] = {
        "identity": identity,
        "cases": summaries,
        "non_worse_count": sum(item["non_worse"] for item in summaries),
        "materially_better_count": sum(item["materially_better"] for item in summaries),
        "accepted": accepted,
        "acceptance_status": "accepted" if accepted else _acceptance_failure_status(validation_errors, summaries),
        "validation_errors": sorted(set(validation_errors)),
        "reviews_sha256": _sha256_path(output_dir / "reviews.json"),
        "scored_at": _utc_now(),
    }
    _write_json(output_dir / "acceptance_report.json", report)
    return report


def _validate_acceptance_run(
    output_dir: Path,
    *,
    identity: dict[str, Any],
    reviews: dict[str, Any],
    mapping: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    try:
        _assert_current_identity(output_dir)
    except RuntimeError as exc:
        errors.append(str(exc))

    expected_ids = [case["id"] for case in FROZEN_CASES]
    if _sha256_text(_canonical_json(mapping)) != identity.get("blind_mapping_sha256"):
        errors.append("blind_mapping_hash_mismatch")
    if sorted(mapping) != sorted(expected_ids):
        errors.append("blind_mapping_case_set_invalid")

    review_manifest_path = output_dir / "reviews_manifest.json"
    if not review_manifest_path.exists():
        errors.append("rating_artifact_hash_missing")
    else:
        review_manifest = _read_json(review_manifest_path)
        if review_manifest.get("reviews_sha256") != _sha256_path(output_dir / "reviews.json"):
            errors.append("rating_artifact_hash_mismatch")
        if review_manifest.get("blind_mapping_sha256") != identity.get("blind_mapping_sha256"):
            errors.append("rating_artifact_mapping_mismatch")
        if review_manifest.get("run_id") != identity.get("run_id"):
            errors.append("rating_artifact_run_mismatch")

    state_path = output_dir / "run_state.json"
    if not state_path.exists():
        errors.append("run_state_missing")
        state: dict[str, Any] = {}
    else:
        state = _read_json(state_path)
        if state.get("completed") != expected_ids:
            errors.append("run_incomplete")
        try:
            _validate_completed_case_hashes(output_dir, state)
        except RuntimeError as exc:
            errors.append(str(exc))

    case_files = sorted((output_dir / "cases").glob("*.json")) if (output_dir / "cases").exists() else []
    if [path.stem for path in case_files] != expected_ids:
        errors.append("case_file_set_invalid")

    errors.extend(_review_validation_errors(reviews))

    for case in FROZEN_CASES:
        case_id = case["id"]
        case_path = output_dir / "cases" / f"{case_id}.json"
        if not case_path.exists():
            errors.append(f"{case_id}:missing_case")
            continue
        artifact = _read_json(case_path)
        errors.extend(_validate_case_artifact(case_id, artifact))

    gate_path = output_dir / "automatic_gates.json"
    if not gate_path.exists():
        errors.append("automatic_gate_report_missing")
    else:
        gates = _read_json(gate_path)
        if gates.get("passed") is not True:
            errors.append("automatic_gates_not_green")
        if gates.get("implementation_manifest_sha256") != identity.get("implementation_manifest_sha256"):
            errors.append("automatic_gate_identity_mismatch")
        checks = gates.get("checks") if isinstance(gates.get("checks"), list) else []
        named_checks = [check for check in checks if isinstance(check, dict)]
        names = [str(check.get("name") or "") for check in named_checks]
        if len(names) != len(set(names)):
            errors.append("automatic_gate_check_names_duplicated")
        by_name = {str(check.get("name") or ""): check for check in named_checks}
        for name in sorted(REQUIRED_AUTOMATIC_GATES):
            check = by_name.get(name)
            if check is None:
                errors.append(f"automatic_gate_check_missing:{name}")
                continue
            check_error = _automatic_check_error(name, check, output_dir)
            if check_error:
                errors.append(f"automatic_gate_check_invalid:{name}:{check_error}")
    return errors


def _review_validation_errors(reviews: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_ids = sorted(case["id"] for case in FROZEN_CASES)
    review_items = [item for item in reviews.get("cases", []) if isinstance(item, dict)]
    review_ids = [str(item.get("id") or "") for item in review_items]
    if sorted(review_ids) != expected_ids or len(review_ids) != len(set(review_ids)):
        return ["review_case_set_invalid"]

    for review in review_items:
        case_id = str(review.get("id") or "")
        choices: list[str] = []
        for criterion in CRITERIA:
            raw_choice = str(review.get(criterion) or "").strip().upper()
            choice = "TIE" if raw_choice in {"TIE", "="} else raw_choice
            choices.append(choice)
            if choice not in {"A", "B", "TIE"}:
                errors.append(f"{case_id}/{criterion} must be A, B, or tie")

        entry_mode = str(review.get("rating_entry_mode") or "per_criterion")
        if entry_mode not in {"per_criterion", "apply_to_all"}:
            errors.append(f"{case_id}:rating_entry_mode_invalid")
        if entry_mode == "apply_to_all":
            source_choice = str(review.get("apply_to_all_source_choice") or "").strip().upper()
            normalized = "TIE" if source_choice in {"TIE", "="} else source_choice
            if normalized not in {"A", "B", "TIE"} or any(value != normalized for value in choices):
                errors.append(f"{case_id}:apply_to_all_audit_invalid")
        if case_id == "LF10" and not isinstance(review.get("unsupported_financing_claim"), bool):
            errors.append("LF10:unsupported_financing_claim_must_be_boolean")
    return errors


def _validate_case_artifact(case_id: str, artifact: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if str(artifact.get("id") or "") != case_id:
        errors.append(f"{case_id}:case_id_mismatch")
    for variant_name in ("legacy", "late_fusion"):
        if not isinstance(artifact.get(variant_name), dict):
            errors.append(f"{case_id}:{variant_name}_missing")
    variant = artifact.get("late_fusion") if isinstance(artifact.get("late_fusion"), dict) else {}
    trace = variant.get("late_fusion") if isinstance(variant.get("late_fusion"), dict) else {}
    if trace.get("pipeline") != "late_fusion":
        errors.append(f"{case_id}:pipeline_not_late_fusion")
    if variant.get("fallback"):
        errors.append(f"{case_id}:fallback_present")
    answer = str(variant.get("answer") or "")
    references = [item for item in variant.get("references") or [] if isinstance(item, dict)]
    reference_ids = [str(item.get("reference_id") or "") for item in references]
    if len(reference_ids) != len(set(reference_ids)) or any(not item for item in reference_ids):
        errors.append(f"{case_id}:reference_ids_invalid")
    cited_ids = {f"S{number}" for number in re.findall(r"\[S(\d+)\]", answer)}
    if references and not cited_ids:
        errors.append(f"{case_id}:citation_missing")
    if cited_ids - set(reference_ids):
        errors.append(f"{case_id}:unknown_citation")
    allowed_urls: set[str] = set()
    for reference in references:
        if not _valid_http_url(reference.get("url")):
            errors.append(f"{case_id}:invalid_reference_url:{reference.get('reference_id')}")
        for key in ("url", "post_url", "primary_url", "youtube_url", "start_url"):
            value = str(reference.get(key) or "").rstrip(".,;:!?)…")
            if _valid_http_url(value):
                allowed_urls.add(value)
        expected_cited = str(reference.get("reference_id") or "") in cited_ids
        if reference.get("cited_in_answer") is not None and reference.get("cited_in_answer") is not expected_cited:
            errors.append(f"{case_id}:cited_in_answer_invalid:{reference.get('reference_id')}")
        searchable = _canonical_json(reference).casefold()
        if "wiki" in searchable:
            errors.append(f"{case_id}:wiki_reference")
    answer_urls = {match.rstrip(".,;:!?)…") for match in re.findall(r"https?://[^\s<>\]\[)}]+", answer)}
    if answer_urls - allowed_urls:
        errors.append(f"{case_id}:invented_answer_url")
    expected_sources = [str(item.get("source_id") or "") for item in references]
    if trace.get("prompt_source_ids") != expected_sources:
        errors.append(f"{case_id}:prompt_reference_mismatch")
    statuses = trace.get("channel_statuses") if isinstance(trace.get("channel_statuses"), dict) else {}
    for channel in ("lightrag", "card_fts", "youtube_fts"):
        status = statuses.get(channel) if isinstance(statuses.get(channel), dict) else {}
        if status.get("status") not in {"success", "empty", "error", "timeout"}:
            errors.append(f"{case_id}:{channel}_status_invalid")
        if not isinstance(status.get("duration_ms"), (int, float)):
            errors.append(f"{case_id}:{channel}_duration_missing")
    if not isinstance(trace.get("estimated_input_tokens"), int) or trace.get("estimated_input_tokens", 0) < 0:
        errors.append(f"{case_id}:token_trace_invalid")
    if trace.get("estimated_input_tokens", 0) > trace.get("max_input_tokens", 0):
        errors.append(f"{case_id}:token_budget_exceeded")
    if not isinstance(trace.get("runtime_context_limit"), int) or trace.get("runtime_context_limit", 0) <= 0:
        errors.append(f"{case_id}:runtime_context_limit_missing")
    if not isinstance(trace.get("output_token_reserve"), int) or trace.get("output_token_reserve", 0) <= 0:
        errors.append(f"{case_id}:output_token_reserve_missing")
    block_tokens = trace.get("source_block_tokens") if isinstance(trace.get("source_block_tokens"), dict) else {}
    if set(block_tokens) != set(reference_ids):
        errors.append(f"{case_id}:source_block_token_set_invalid")
    for reference_id, token_counts in block_tokens.items():
        if not isinstance(token_counts, dict) or any(
            not isinstance(token_counts.get(key), int) or token_counts.get(key, -1) < 0
            for key in ("full", "final")
        ):
            errors.append(f"{case_id}:source_block_tokens_invalid:{reference_id}")
    drops = trace.get("dropped_source_ids") if isinstance(trace.get("dropped_source_ids"), list) else []
    if any(
        not isinstance(item, dict)
        or not str(item.get("source_id") or "").strip()
        or not str(item.get("reason") or "").strip()
        for item in drops
    ):
        errors.append(f"{case_id}:dropped_source_trace_invalid")
    return errors


def _acceptance_failure_status(errors: list[str], summaries: list[dict[str, Any]]) -> str:
    if any("identity" in error or "hash" in error for error in errors):
        return "invalid_run"
    if any("missing" in error or "incomplete" in error or "case_set" in error for error in errors):
        return "incomplete_run"
    return "failed_gate" if errors or summaries else "incomplete_run"


def _result_artifact(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result, dict) else {}
    return {
        "answer": str(result.get("llm_response", {}).get("content") or result.get("response") or ""),
        "references": list(data.get("references") or []) if isinstance(data, dict) else [],
        "late_fusion": dict(data.get("late_fusion") or {}) if isinstance(data, dict) else {},
        "fallback": result.get("fallback") if isinstance(result, dict) else None,
    }


def _blind_mapping(seed: str) -> dict[str, dict[str, str]]:
    mapping = {}
    for case in FROZEN_CASES:
        labels = ["legacy", "late_fusion"]
        random.Random(f"{seed}:{case['id']}").shuffle(labels)
        mapping[case["id"]] = {"A": labels[0], "B": labels[1]}
    return mapping


def _write_blind_case(
    output_dir: Path,
    case_id: str,
    artifact: dict[str, Any],
    *,
    mapping: dict[str, Any],
) -> None:
    case_mapping = mapping[case_id]
    _write_json(
        output_dir / "blind" / f"{case_id}.json",
        {
            "id": case_id,
            "question": artifact["question"],
            "A": _blind_variant_artifact(artifact[case_mapping["A"]]),
            "B": _blind_variant_artifact(artifact[case_mapping["B"]]),
        },
    )


def write_blind_review_packet(output_dir: Path) -> Path:
    """Render answer pairs without revealing which variant is Late Fusion."""
    mapping = _read_json(output_dir / "blind_mapping.json")
    lines = [
        "# Late-Fusion A/B blind review",
        "",
        "Do not open `blind_mapping.json` before choosing A, B or tie for each criterion in `reviews.json`.",
        "Review completeness, specificity, relevance, absence of unsupported claims, and citations/source links.",
        "",
    ]
    for case in FROZEN_CASES:
        artifact = _read_json(output_dir / "cases" / f"{case['id']}.json")
        case_mapping = mapping[case["id"]]
        lines.extend(
            [
                f"## {case['id']}",
                "",
                f"Question: {case['question']}",
                "",
                "### A",
                "",
                str(artifact[case_mapping["A"]]["answer"]),
                "",
                *_render_blind_references(artifact[case_mapping["A"]]),
                "",
                "### B",
                "",
                str(artifact[case_mapping["B"]]["answer"]),
                "",
                *_render_blind_references(artifact[case_mapping["B"]]),
                "",
            ]
        )
    packet_path = output_dir / "blind" / "REVIEW.md"
    _write_text_atomic(packet_path, "\n".join(lines) + "\n")
    return packet_path


def _blind_variant_artifact(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": str(variant.get("answer") or ""),
        "references": [
            {
                "reference_id": str(reference.get("reference_id") or ""),
                "title": str(reference.get("title") or ""),
                "url": str(reference.get("url") or ""),
                "post_url": str(reference.get("post_url") or ""),
                "primary_url": str(reference.get("primary_url") or ""),
                "youtube_url": str(reference.get("youtube_url") or ""),
                "content_type": str(reference.get("content_type") or ""),
                "start_url": str(reference.get("start_url") or ""),
            }
            for reference in variant.get("references") or []
            if isinstance(reference, dict)
        ],
    }


def _render_blind_references(variant: dict[str, Any]) -> list[str]:
    references = [reference for reference in variant.get("references") or [] if isinstance(reference, dict)]
    lines = ["References:", ""]
    if not references:
        return [*lines, "- None"]
    answer = str(variant.get("answer") or "")
    cited: list[dict[str, Any]] = []
    uncited: list[dict[str, Any]] = []
    for reference in references:
        reference_id = str(reference.get("reference_id") or "")
        (cited if f"[{reference_id}]" in answer else uncited).append(reference)

    if cited:
        lines.extend(["Cited in the answer:", ""])
        lines.extend(_render_blind_reference(reference) for reference in cited)
    if uncited:
        if cited:
            lines.append("")
        lines.extend(
            [
                "Additional context sources:",
                "",
                "These are validated sources supplied to the model. Their citation IDs are not printed in the answer; this does not mean the sources are unverified.",
                "",
            ]
        )
        lines.extend(_render_blind_reference(reference, uncited=True) for reference in uncited)
    return lines


def _render_blind_reference(reference: dict[str, Any], *, uncited: bool = False) -> str:
    reference_id = str(reference.get("reference_id") or "")
    title = str(reference.get("title") or "Источник").replace("\n", " ")
    url = _canonical_reference_url(reference)
    content_type = str(reference.get("content_type") or "n/a").replace("\n", " ")
    start_url = str(reference.get("start_url") or "").strip()
    title_part = (
        f"[{title}]({url})"
        if url
        else f"{title} — no canonical link was exported in the frozen response metadata"
    )
    details = [f"content type: {content_type}"]
    if _valid_http_url(start_url):
        details.append(f"[timestamp/start URL]({start_url})")
    if uncited:
        details.append("citation ID absent from answer")
    if not url:
        details.append("retrieved context, not an unsupported-claim marker")
    return f"- [{reference_id}] {title_part} — {'; '.join(details)}"


def _canonical_reference_url(reference: dict[str, Any]) -> str:
    for key in ("url", "primary_url", "post_url", "youtube_url", "start_url"):
        value = str(reference.get(key) or "").strip()
        if _valid_http_url(value):
            return value
    return ""


def finalize_reviews(output_dir: Path) -> dict[str, Any]:
    """Freeze completed human ratings before the fail-closed scorer reads them."""
    identity = _assert_current_identity(output_dir)
    reviews_path = output_dir / "reviews.json"
    reviews = _read_json(reviews_path)
    validation_errors = _review_validation_errors(reviews)
    if validation_errors:
        raise RuntimeError("Cannot finalize reviews: " + "; ".join(validation_errors))
    manifest = {
        "run_id": identity.get("run_id"),
        "reviews_sha256": _sha256_path(reviews_path),
        "blind_mapping_sha256": identity.get("blind_mapping_sha256"),
        "finalized_at": _utc_now(),
    }
    manifest_path = output_dir / "reviews_manifest.json"
    if manifest_path.exists():
        existing = _read_json(manifest_path)
        comparable = {key: existing.get(key) for key in manifest if key != "finalized_at"}
        expected = {key: manifest.get(key) for key in manifest if key != "finalized_at"}
        if comparable != expected:
            raise RuntimeError("Finalized review artifact changed; create a new review finalization.")
        return existing
    _write_json(manifest_path, manifest)
    return manifest


def _review_template() -> dict[str, Any]:
    return {
        "instructions": "Before opening blind_mapping.json, enter A, B, or tie for every criterion. The scorer converts these blind choices into Late-Fusion-versus-legacy values only after review.",
        "cases": [
            {
                "id": case["id"],
                **{criterion: None for criterion in CRITERIA},
                "rating_entry_mode": "per_criterion",
                "apply_to_all_source_choice": None,
                "unsupported_financing_claim": None if case["id"] == "LF10" else False,
            }
            for case in FROZEN_CASES
        ],
    }


def _score_blind_choice(value: Any, mapping: dict[str, str], case_id: str, criterion: str) -> int:
    choice = str(value or "").strip().upper()
    if choice in {"TIE", "="}:
        return 0
    if choice not in {"A", "B"}:
        raise ValueError(f"{case_id}/{criterion} must be A, B, or tie")
    return 1 if mapping[choice] == "late_fusion" else -1


def _directory_manifest_hash(path: Path, *, ignored_relative_paths: set[str] | None = None) -> str:
    if not path.exists():
        return "missing"
    ignored = {item.replace("\\", "/") for item in (ignored_relative_paths or set())}
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()), key=lambda value: str(value).casefold()
    ):
        relative_path = str(item.relative_to(path)).replace("\\", "/")
        if relative_path in ignored:
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(_sha256_path(item).encode("ascii"))
    return digest.hexdigest()


def _implementation_manifest() -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for relative_path in sorted(SCOPED_PATHS, key=str.casefold):
        path = PROJECT_ROOT / relative_path
        status_text = _git_output("status", "--short", "--", relative_path)
        if status_text.startswith("??"):
            file_status = "untracked"
        elif status_text and status_text != "unavailable":
            file_status = "modified"
        else:
            file_status = "tracked" if _git_output("ls-files", "--", relative_path) else "missing"
        manifest.append(
            {
                "relative_path": relative_path.replace("\\", "/"),
                "file_status": file_status,
                "size_bytes": path.stat().st_size if path.is_file() else 0,
                "sha256": _sha256_path(path),
            }
        )
    return manifest


def _sha256_path(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip() if completed.returncode == 0 else "unavailable"


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _assert_current_identity(output_dir: Path) -> dict[str, Any]:
    identity = _read_json(output_dir / "identity.json")
    current = build_identity(
        seed=str(identity.get("run_seed") or ""),
        started_at=str(identity.get("started_at") or ""),
        run_id=str(identity.get("run_id") or ""),
    )
    if current != identity:
        raise RuntimeError("run_identity_changed")
    return identity


def _validate_completed_case_hashes(output_dir: Path, state: dict[str, Any]) -> None:
    hashes = state.get("case_sha256") if isinstance(state.get("case_sha256"), dict) else {}
    for case_id in state.get("completed") or []:
        case_path = output_dir / "cases" / f"{case_id}.json"
        expected = str(hashes.get(case_id) or "")
        if not case_path.is_file() or not expected or _sha256_path(case_path) != expected:
            raise RuntimeError(f"completed_case_hash_mismatch:{case_id}")


def _valid_http_url(value: Any) -> bool:
    from urllib.parse import urlsplit

    raw = str(value or "").strip()
    if not raw or any(ord(character) < 32 for character in raw):
        return False
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def record_automatic_gates(output_dir: Path, checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind successful local verification evidence to the immutable A/B identity."""
    identity = _assert_current_identity(output_dir)
    by_name = {str(check.get("name") or ""): check for check in checks if isinstance(check, dict)}
    names = [str(check.get("name") or "") for check in checks if isinstance(check, dict)]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    missing = sorted(REQUIRED_AUTOMATIC_GATES - set(by_name))
    failed = sorted(
        name
        for name in REQUIRED_AUTOMATIC_GATES & set(by_name)
        if _automatic_check_error(name, by_name[name], output_dir)
    )
    report = {
        "passed": not missing and not failed and not duplicated,
        "missing": missing,
        "failed": failed,
        "duplicated": duplicated,
        "checks": checks,
        "implementation_manifest_sha256": identity.get("implementation_manifest_sha256"),
        "recorded_at": _utc_now(),
    }
    _write_json(output_dir / "automatic_gates.json", report)
    return report


def _automatic_check_error(name: str, check: dict[str, Any], output_dir: Path) -> str:
    if check.get("exit_code") != 0 or not str(check.get("command") or "").strip():
        return "command_failed"
    log_path_value = str(check.get("log_path") or "").strip()
    if not log_path_value:
        return "log_missing"
    log_path = Path(log_path_value)
    if not log_path.is_absolute():
        log_path = output_dir / log_path
    if not log_path.is_file() or check.get("log_sha256") != _sha256_path(log_path):
        return "log_hash_mismatch"
    if name in {"full_suite_flag_false", "full_suite_flag_true"}:
        environment = check.get("environment") if isinstance(check.get("environment"), dict) else {}
        expected_flag = "false" if name.endswith("false") else "true"
        if str(environment.get("LATE_FUSION_ENABLED") or "").casefold() != expected_flag:
            return "flag_environment_invalid"
        if str(environment.get("LLM_PROFILE") or "").casefold() != "current":
            return "llm_profile_environment_invalid"
        if not str(environment.get("LLM_MODEL") or "").strip():
            return "llm_model_environment_missing"
    return ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or score the frozen Late-Fusion A/B contract.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "artifacts" / "late_fusion_ab")
    parser.add_argument("--resume", action="store_true", help="Resume only when identity.json is identical.")
    parser.add_argument("--seed", default="late-fusion-v1", help="Stable blind-pair seed.")
    parser.add_argument("--score", action="store_true", help="Score existing reviews.json instead of running models.")
    parser.add_argument(
        "--finalize-reviews",
        action="store_true",
        help="Freeze reviews.json by hash before scoring.",
    )
    parser.add_argument(
        "--render-review", action="store_true", help="Render a blind Markdown packet from completed cases."
    )
    args = parser.parse_args()
    if args.finalize_reviews:
        manifest = finalize_reviews(args.output_dir)
        print(f"Reviews finalized: {manifest['reviews_sha256']}")
        return
    if args.score:
        report = score_reviews(args.output_dir)
        print(
            f"A/B accepted={report['accepted']} non_worse={report['non_worse_count']} materially_better={report['materially_better_count']}"
        )
        return
    if args.render_review:
        review_path = args.output_dir / "reviews.json"
        if not review_path.exists():
            _write_json(review_path, _review_template())
        print(f"Blind review packet: {write_blind_review_packet(args.output_dir)}")
        return
    report = asyncio.run(run_cases(args.output_dir, resume=args.resume, seed=args.seed))
    print(f"A/B completed={len(report['completed'])}/{len(FROZEN_CASES)} complete={report['complete']}")


if __name__ == "__main__":
    main()
