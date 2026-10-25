"""Synchronize deterministic media flags/issues from normalized sidecars to cards."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.contracts.models import (
    EnrichedCardV2,
    NormalizedMeta,  # noqa: E402
)
from geospoiler.ingestion.enricher.pipeline import (
    _media_quality_flags,
    _media_quality_issues,  # noqa: E402
)
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_bytes,
    atomic_write_release_json,
    build_identity,
    preflight_release_identity,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
)
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file  # noqa: E402

MEDIA_CONTRACT_VERSION = "media-contract-v2"
MEDIA_FLAGS = {"transcript_unavailable"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _is_dedicated_youtube_only(meta: NormalizedMeta) -> bool:
    """Dedicated YouTube artifacts are synchronized by the YouTube pipeline."""
    return not (meta.media or []) and bool(getattr(meta, "youtube_sources", None))


def sync_media_contract(
    normalized_dir: Path | None = None,
    enriched_dir: Path | None = None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    normalized_dir = (normalized_dir or config.NORMALIZED_DIR).resolve()
    enriched_dir = (enriched_dir or config.ENRICHED_DIR).resolve()
    changed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    mismatches = 0
    for meta_path in sorted(normalized_dir.rglob("*.meta.json")):
        text_name = meta_path.name[: -len(".meta.json")] + ".enriched.json"
        card_path = enriched_dir / meta_path.relative_to(normalized_dir).parent / text_name
        try:
            meta = NormalizedMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(
                {
                    "path": str(meta_path),
                    "error": safe_exception_token(
                        "invalid_media_meta",
                        exc,
                        component="sync_media_contract",
                    ),
                }
            )
            continue
        if not card_path.is_file():
            if _is_dedicated_youtube_only(meta):
                continue
            errors.append({"path": str(meta_path), "error": "missing_card"})
            continue
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(
                {
                    "path": str(meta_path),
                    "error": safe_exception_token(
                        "invalid_media_card",
                        exc,
                        component="sync_media_contract",
                    ),
                }
            )
            continue
        expected_flags = _media_quality_flags(meta)
        expected_issues = _media_quality_issues(meta)
        current_flags = list(card.quality_flags or [])
        current_issues = list(card.extraction_issues or [])
        new_flags = list(dict.fromkeys([flag for flag in current_flags if flag not in MEDIA_FLAGS] + expected_flags))
        new_issues = list(
            dict.fromkeys([issue for issue in current_issues if not str(issue).startswith("media:")] + expected_issues)
        )
        if (
            new_flags == current_flags
            and new_issues == current_issues
            and card.media_contract_version == MEDIA_CONTRACT_VERSION
        ):
            continue
        mismatches += 1
        before = _sha256(card_path)
        card.quality_flags = new_flags
        card.extraction_issues = new_issues
        card.media_contract_version = MEDIA_CONTRACT_VERSION
        if apply:
            _atomic_write(card_path, card.model_dump(mode="json"))
        changed.append(
            {
                "card_path": str(card_path),
                "before_sha256": before,
                "after_sha256": _sha256(card_path) if apply else None,
                "flags": expected_flags,
                "issues": expected_issues,
            }
        )
    return {
        "schema_version": "media_contract_sync_v1",
        "status": "failed"
        if errors
        else ("needs_downstream_rebuild" if apply and changed else ("changes_pending" if changed else "passed")),
        "apply": apply,
        "candidates": len(changed),
        "changed": sum(1 for item in changed if item["after_sha256"] is not None),
        "mismatches": mismatches,
        "errors": errors,
        "changes_pending": bool(changed and not apply),
        "downstream_rebuild_required": bool(apply and changed),
        "items": changed,
    }


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="publish deterministic card updates")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="accepted_manifest_v2 used for release identity")
    parser.add_argument("--release-run-id", help="must match the manifest run_id")
    args = parser.parse_args()
    if bool(args.manifest) != bool(args.release_run_id):
        parser.error("--manifest and --release-run-id must be supplied together")
    safe_resolve_release_path(args.report, label="media_contract_report_path")
    preflight = None
    if args.manifest is not None:
        validation = load_manifest_file(
            safe_resolve_release_path(args.manifest, label="release_manifest_path")
        )
        preflight = preflight_release_identity(
            args.release_run_id,
            args.manifest,
            validation=validation,
            identity_builder=build_identity,
        )
    report = sync_media_contract(apply=args.apply)
    if preflight is not None:
        identity = (
            build_identity(
                args.release_run_id,
                preflight.manifest_path,
                preflight.manifest,
                manifest_bytes=preflight.manifest_bytes,
            )
            if args.apply
            else preflight.identity
        )
        report = stamp_report(
            report,
            identity,
            "python scripts/sync_media_contract.py"
            f" --report {args.report} --manifest {args.manifest}"
            f" --release-run-id {args.release_run_id}"
            + (" --apply" if args.apply else ""),
            producer="scripts.sync_media_contract",
            producer_schema_version="media_contract_sync_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=[
                "python", "scripts/sync_media_contract.py", "--report", str(args.report),
                "--manifest", str(args.manifest), "--release-run-id", args.release_run_id,
                *(["--apply"] if args.apply else []),
            ],
        )
    atomic_write_release_json(args.report, report, label="media_contract_report")
    print(json.dumps({key: report[key] for key in ("status", "apply", "candidates", "changed", "mismatches")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="sync_media_contract"))
