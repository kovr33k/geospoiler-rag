"""Live activation smoke for the active Late-Fusion runtime configuration."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.answering.query import query_rag_result  # noqa: E402
from geospoiler.cli.runtime import _finalize_rag_safely  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.ingestion.normalizer.youtube_handler import redact_sensitive_url  # noqa: E402
from geospoiler.operations.late_fusion_ab import (
    _result_artifact,
    _valid_http_url,  # noqa: E402
)
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_release_json,
    best_effort_with_pending_interrupt,
    build_identity,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_exception_diagnostic,
    safe_resolve_release_path,
    sanitize_release_value,
    sha256_bytes,
    stamp_report,
    strict_json_loads,
)
from geospoiler.operations.smoke_contract import CASES, OFFICIAL_CASES_SHA256, smoke_attempt_chain_errors
from geospoiler.retrieval.lightrag.factory import create_rag  # noqa: E402
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file  # noqa: E402

_CITATION_RE = re.compile(r"\[(S\d+)\]")


def _timestamp_aliases(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return ()
    total_seconds = int(value)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return tuple(
        dict.fromkeys(
            (
                f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                f"{hours * 60 + minutes:02d}:{seconds:02d}",
            )
        )
    )


def _youtube_timestamp_contract_errors(
    answer: str,
    references: list[dict[str, Any]],
) -> list[str]:
    # Older signed smoke fixtures predate segment-level metadata. Runtime R26
    # references always include the field; keep historical artifact validation
    # readable while enforcing the stronger contract on new results.
    if not any("segment_start_urls" in reference for reference in references):
        return []
    errors: list[str] = []
    mentioned_segment_starts = 0
    for reference in references:
        reference_id = str(reference.get("reference_id") or "")
        for segment in reference.get("segment_start_urls", []):
            if not isinstance(segment, dict):
                continue
            aliases = _timestamp_aliases(segment.get("start_seconds"))
            if not aliases or not any(alias in answer for alias in aliases):
                continue
            mentioned_segment_starts += 1
            start_url = str(segment.get("start_url") or "")
            if not start_url or start_url not in answer:
                start_seconds = segment.get("start_seconds")
                errors.append(
                    f"youtube_timestamp_url_missing:{reference_id}:{start_seconds}"
                )
    if mentioned_segment_starts < 2:
        errors.append("youtube_timestamp_coverage_insufficient")
    return errors


def _finalize_smoke_attempt(
    *,
    output_path: Path,
    attempt_history_path: Path | None,
    preflight: Any,
    attempts: list[dict[str, Any]],
    attempt_index: int,
    attempt_id: str,
    started_at: str,
    cases: list[dict[str, Any]],
    redaction_count: int,
    run_error: BaseException | None,
) -> dict[str, Any]:
    run_diagnostic = (
        safe_exception_diagnostic(
            "late_fusion_smoke_failed",
            run_error,
            component="late_fusion_smoke",
        )
        if run_error is not None
        else None
    )
    report_errors = []
    if redaction_count:
        report_errors.append(f"release_secret_material_redacted:count={redaction_count}")
    if run_diagnostic is not None:
        report_errors.append(
            ":".join(
                (
                    run_diagnostic["error_code"],
                    run_diagnostic["exception_type"],
                    run_diagnostic["message_sha256"],
                )
            )
        )
    report = {
        "schema_version": "late_fusion_smoke_v1",
        "late_fusion_enabled": config.LATE_FUSION_ENABLED,
        "reranker_enabled": config.RERANKER_ENABLED,
        "wiki_enabled": config.WIKI_ENABLED,
        "cases": cases,
        "cases_sha256": sha256_bytes(canonical_json(cases)),
        "official_cases_sha256": OFFICIAL_CASES_SHA256,
        "passed": (
            run_error is None
            and redaction_count == 0
            and len(cases) == len(CASES)
            and all(not case["errors"] for case in cases)
        ),
        "errors": report_errors,
    }
    attempt = {
        "attempt_index": attempt_index,
        "attempt_id": attempt_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "passed": report["passed"],
        "case_count": len(cases),
        "cases_sha256": report["cases_sha256"],
        "errors": [error for case in cases for error in case["errors"]] + report["errors"],
    }
    attempt["previous_attempt_sha256"] = (
        attempts[-1].get("attempt_sha256") if attempts else None
    )
    attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
    attempts.append(attempt)
    if smoke_attempt_chain_errors(attempts):  # pragma: no cover - producer invariant
        raise RuntimeError("smoke_attempt_chain_invalid") from None
    report["attempt_id"] = attempt_id
    report["attempts"] = attempts
    report["attempt_history_path"] = (
        str(attempt_history_path) if attempt_history_path is not None else None
    )
    if attempt_history_path is not None:
        assert_no_release_secrets(canonical_json(attempts), label="smoke_attempt_ledger")
        atomic_write_release_json(
            attempt_history_path, attempts, label="smoke_attempt_ledger"
        )
    if preflight is not None:
        identity = preflight.identity
        report = stamp_report(
            report,
            identity,
            "python scripts/late_fusion_activation_smoke.py",
            producer="scripts.late_fusion_activation_smoke",
            producer_schema_version="late_fusion_smoke_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=["python", "scripts/late_fusion_activation_smoke.py"],
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_release_secrets(canonical_json(report), label="late_fusion_smoke_report")
    atomic_write_release_json(output_path, report, label="late_fusion_smoke_report")
    return report


async def run_smoke(
    output_path: Path,
    *,
    manifest_path: Path | None = None,
    release_run_id: str | None = None,
    attempt_history_path: Path | None = None,
) -> dict[str, Any]:
    if not config.LATE_FUSION_ENABLED:
        raise RuntimeError("Active Late-Fusion flag is disabled")
    if config.RERANKER_ENABLED:
        raise RuntimeError("Reranker must remain disabled for this activation")

    output_path = safe_resolve_release_path(
        output_path,
        label="release_output_path",
    )
    preflight = None
    if manifest_path is not None:
        if not release_run_id:
            raise ValueError("release_run_id is required with --manifest")
        output_parent = output_path.parent
        derived_history_path = output_parent / "smoke_attempts.json"
        if attempt_history_path is not None and safe_resolve_release_path(
            attempt_history_path,
            label="smoke_attempt_history_path",
        ) != derived_history_path:
            raise ValueError("release smoke attempt history path is derived from the release run directory")
        attempt_history_path = derived_history_path
        history_parent = safe_resolve_release_path(
            attempt_history_path,
            label="smoke_attempt_history_path",
        ).parent
        if history_parent != output_parent:
            raise ValueError("release smoke attempt history must live beside the output report")
        validation = load_manifest_file(
            safe_resolve_release_path(
                manifest_path,
                label="release_manifest_path",
            )
        )
        preflight = preflight_release_identity(
            release_run_id,
            manifest_path,
            validation=validation,
            identity_builder=build_identity,
        )
    attempts: list[dict[str, Any]] = []
    if attempt_history_path is not None and attempt_history_path.is_file():
        try:
            previous_bytes = attempt_history_path.read_bytes()
            assert_no_release_secrets(previous_bytes, label="smoke_attempt_history")
            previous = strict_json_loads(previous_bytes)
        except (OSError, UnicodeError, ValueError):
            raise ValueError("smoke_attempt_history_invalid") from None
        if smoke_attempt_chain_errors(previous):
            raise ValueError("smoke_attempt_history_invalid") from None
        attempts.extend(previous)
    attempt_index = len(attempts) + 1
    attempt_id = f"attempt-{attempt_index:03d}"
    started_at = datetime.now(UTC).isoformat()
    rag = None
    cases: list[dict[str, Any]] = []
    redaction_count = 0
    run_error: BaseException | None = None
    try:
        rag = await create_rag()
        for case_id, question, profile in CASES:
            result = await query_rag_result(rag, question, mode="mix", query_profile=profile)
            artifact, artifact_redactions = sanitize_release_value(_result_artifact(result))
            if not isinstance(artifact, dict):
                raise RuntimeError("release smoke sanitization returned an invalid artifact")
            redaction_count += artifact_redactions
            answer = artifact["answer"]
            references = artifact["references"]
            trace = artifact["late_fusion"]
            reference_ids = {str(item.get("reference_id") or "") for item in references}
            citations = set(_CITATION_RE.findall(answer))
            errors: list[str] = []
            if artifact_redactions:
                errors.append(f"release_secret_material_redacted:count={artifact_redactions}")
            if trace.get("pipeline") != "late_fusion":
                errors.append("pipeline_not_late_fusion")
            if artifact["fallback"]:
                errors.append("fallback_present")
            if not references:
                errors.append("references_empty")
            if not citations:
                errors.append("citations_empty")
            if citations - reference_ids:
                errors.append("unknown_citations")
            if any(not _valid_http_url(str(item.get("url") or "")) for item in references):
                errors.append("invalid_reference_url")
            if case_id == "SMOKE_YOUTUBE" and not any(item.get("start_url") for item in references):
                errors.append("youtube_start_url_missing")
            if case_id == "SMOKE_YOUTUBE":
                errors.extend(_youtube_timestamp_contract_errors(answer, references))
            cases.append(
                {
                    "id": case_id,
                    "question": question,
                    "profile": profile,
                    "answer": answer,
                    "reference_count": len(references),
                    "citation_count": len(citations),
                    "citation_ids": sorted(citations),
                    "youtube_start_url_count": sum(bool(item.get("start_url")) for item in references),
                    "pipeline": trace.get("pipeline"),
                    "fallback_reason": trace.get("fallback_reason"),
                    "fallback": bool(artifact["fallback"]),
                    "channel_statuses": trace.get("channel_statuses"),
                    "references": [
                        {
                            "reference_id": item.get("reference_id"),
                            "url": redact_sensitive_url(str(item.get("url") or "")),
                            "start_url": redact_sensitive_url(str(item.get("start_url") or "")) if item.get("start_url") else None,
                            "segment_start_urls": [
                                {
                                    "segment_id": segment.get("segment_id"),
                                    "segment_index": segment.get("segment_index"),
                                    "start_seconds": segment.get("start_seconds"),
                                    "end_seconds": segment.get("end_seconds"),
                                    "start_url": redact_sensitive_url(str(segment.get("start_url") or "")),
                                }
                                for segment in item.get("segment_start_urls", [])
                                if isinstance(segment, dict) and segment.get("start_url")
                            ],
                        }
                        for item in references
                    ],
                    "errors": errors,
                }
            )
    except BaseException as exc:  # persist the failed attempt before propagating
        run_error = exc
    finally:
        if rag is not None:
            try:
                await _finalize_rag_safely(rag)
            except BaseException as exc:
                if run_error is None:
                    run_error = exc

    pending_interrupt = (
        run_error
        if isinstance(run_error, (KeyboardInterrupt, asyncio.CancelledError))
        else None
    )
    report = best_effort_with_pending_interrupt(
        pending_interrupt,
        lambda: _finalize_smoke_attempt(
            output_path=output_path,
            attempt_history_path=attempt_history_path,
            preflight=preflight,
            attempts=attempts,
            attempt_index=attempt_index,
            attempt_id=attempt_id,
            started_at=started_at,
            cases=cases,
            redaction_count=redaction_count,
            run_error=run_error,
        ),
    )
    if run_error is not None:
        if isinstance(run_error, (KeyboardInterrupt, asyncio.CancelledError)):
            raise run_error
        run_diagnostic = safe_exception_diagnostic(
            "late_fusion_smoke_failed",
            run_error,
            component="late_fusion_smoke",
        )
        if run_diagnostic is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("late_fusion_smoke_failed") from None
        raise RuntimeError(
            ":".join(
                (
                    run_diagnostic["error_code"],
                    run_diagnostic["exception_type"],
                    run_diagnostic["message_sha256"],
                )
            )
        ) from None
    if not isinstance(report, dict):  # pragma: no cover - defensive invariant
        raise RuntimeError("late_fusion_smoke_finalization_missing") from None
    return report


def main() -> None:
    parser = SafeArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-run-id")
    parser.add_argument("--attempt-history", type=Path)
    args = parser.parse_args()
    if args.manifest is not None and args.attempt_history is not None:
        parser.error("--attempt-history is derived from the release output directory and cannot be overridden")
    report = asyncio.run(
        run_smoke(
            args.output,
            manifest_path=args.manifest,
            release_run_id=args.release_run_id,
            attempt_history_path=args.attempt_history,
        )
    )
    for case in report["cases"]:
        print(
            f"{case['id']}: pipeline={case['pipeline']} refs={case['reference_count']} "
            f"citations={case['citation_count']} fallback={bool(case['fallback'])} errors={case['errors']}"
        )
    print(f"activation_smoke_passed={report['passed']}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(
        run_release_cli(main, component="late_fusion_activation_smoke")
    )
