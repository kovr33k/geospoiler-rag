"""Build a fail-closed, immutable release verification report."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.contracts.models import (
    EnrichedCardV2,
    NormalizedMeta,
    SourceId,
    YouTubeSegmentCardV2,  # noqa: E402
)
from geospoiler.ingestion.enricher.pipeline import (
    _PIPELINE_OWNED_QUALITY_FLAGS,
    _media_entries,
    _media_quality_flags,
    _media_quality_issues,
)
from geospoiler.operations import (
    migrate_portable_paths,
    security_release_scan,
    validate_backup_restore,  # noqa: E402
)
from geospoiler.operations.build_release_risk_register import validate_risk_register_payload  # noqa: E402
from geospoiler.operations.cold_provenance import compute_source_proof, loaded_doc_proof, storage_inventory
from geospoiler.operations.graph_provenance_reconcile import _state_snapshot  # noqa: E402
from geospoiler.operations.graph_provenance_reconcile import reconcile as reconcile_current  # noqa: E402
from geospoiler.operations.release_identity import (
    RELEASE_EVIDENCE_SCHEMA,
    RELEASE_REPORT_KINDS,
    RELEASE_TEST_TIMEOUT_SECONDS,
    TRUSTED_RELEASE_BASE_REF,
    ReleasePublicationCommittedError,
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_release_json,
    best_effort_with_pending_interrupt,
    canonical_json,
    capture_release_artifact_tree_snapshot,
    config_identity_sha256,
    durable_prepare_tree,
    git_commit,
    git_status_porcelain,
    git_tree_sha256,
    materialize_release_artifact_tree_snapshot,
    prepared_publication_tree,
    publication_outcome,
    release_git_snapshot,
    run_release_cli,
    runtime_identity,
    safe_exception_diagnostic,
    safe_exception_token,
    safe_resolve_release_path,
    sanitize_release_value,
    scan_release_artifact_tree,
    sha256_bytes,
    sha256_file,
    static_environment_receipt_errors,
    strict_json_loads,
    validate_manifest_run_id,
    validate_report_envelope,
    working_tree_sha256,
)
from geospoiler.operations.smoke_contract import CASES as OFFICIAL_SMOKE_CASES  # noqa: E402
from geospoiler.operations.smoke_contract import OFFICIAL_CASES_SHA256, smoke_attempt_chain_is_valid
from geospoiler.providers import llm_backend  # noqa: E402
from geospoiler.retrieval.cards.card_fts import card_to_fts_record  # noqa: E402
from geospoiler.retrieval.lightrag.manifest_contract import (
    load_manifest_file,
    validate_manifest_payload,  # noqa: E402
)
from geospoiler.retrieval.lightrag.storage import (
    _canonical_source_path,
    _rag_file_path,  # noqa: E402
)
from geospoiler.sources.registry import (
    _compute_content_hash,
    _references_from_enriched_card,
    _references_from_normalized_meta,
)

MEDIA_CONTRACT_VERSION = "media-contract-v2"


def _normalise_index_path(value: str) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return str(candidate.resolve(strict=False))


def _exact_index_reconciliation(
    enriched_cards: list[Path],
    normalized_meta: list[Path],
    snapshot: dict[str, Any],
    youtube_segments: list[Path] | None = None,
) -> tuple[bool, list[str]]:
    """Compare identity-bearing FTS/registry/metadata rows, not just counts."""
    errors: list[str] = []
    expected_sources_by_id: dict[str, tuple[str, str, str]] = {}
    expected_normalized: set[tuple[str, str]] = set()
    expected_enriched: set[tuple[str, str, str]] = set()
    expected_fts: set[tuple[Any, ...]] = set()
    expected_youtube: set[tuple[Any, ...]] = set()
    expected_references: list[tuple[str, str, str, str]] = []
    expected_paths: set[str] = set()
    card_ids: set[str] = set()
    for card_path in enriched_cards:
        try:
            card = _read_json(card_path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(
                f"{card_path}:"
                + safe_exception_token(
                    "card_identity_unreadable",
                    exc,
                    component="release_verifier",
                )
            )
            continue
        provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
        source_id = str(provenance.get("source_id") or "")
        normalized_file = str(provenance.get("normalized_path") or "")
        card_file = str(card_path)
        post_url = str(provenance.get("post_url") or "")
        if not source_id or source_id in card_ids:
            errors.append(f"card_identity_duplicate_or_empty:{card_path}")
            continue
        card_ids.add(source_id)
        expected_sources_by_id[source_id] = (normalized_file, card_file, post_url)
        expected_enriched.add((source_id, card_file, _compute_content_hash(card)))
        canonical = _canonical_source_path(normalized_file) if normalized_file else ""
        if canonical:
            expected_paths.add(canonical)
            expected_paths.add(_canonical_source_path(_rag_file_path(canonical)))
        fts_record = card_to_fts_record(card, card_path)
        if fts_record is not None:
            expected_fts.add(
                (
                    fts_record.source_id,
                    _normalise_index_path(fts_record.card_path),
                    _normalise_index_path(fts_record.normalized_file),
                    fts_record.post_url,
                    fts_record.title,
                    fts_record.search_text,
                    fts_record.entities,
                    fts_record.topics,
                    fts_record.claim_types,
                )
            )
        expected_references.extend(
            (source_id, str(item.get("reference_type") or ""), str(item.get("url") or ""), str(item.get("label") or ""))
            for item in _references_from_enriched_card(source_id, card)
        )
    for meta_path in normalized_meta:
        try:
            meta = _read_json(meta_path)
        except (OSError, UnicodeError, ValueError):
            continue
        source_identity = SourceId.from_provenance(meta)
        source_id = source_identity.value if source_identity else ""
        if source_id:
            normalized_file = str(meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".txt"))
            if source_id not in expected_sources_by_id:
                expected_sources_by_id[source_id] = (normalized_file, "", str(meta.get("post_url") or ""))
            expected_normalized.add((source_id, _normalise_index_path(normalized_file)))
            canonical = _canonical_source_path(normalized_file)
            expected_paths.add(canonical)
            expected_paths.add(_canonical_source_path(_rag_file_path(canonical)))
            expected_references.extend(
                (source_id, str(item.get("reference_type") or ""), str(item.get("url") or ""), str(item.get("label") or ""))
                for item in _references_from_normalized_meta(source_id, meta)
            )
    for segment_path in youtube_segments or []:
        try:
            segment = YouTubeSegmentCardV2.model_validate(_read_json(segment_path))
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            errors.append(
                f"{segment_path}:"
                + safe_exception_token(
                    "youtube_identity_unreadable",
                    exc,
                    component="release_verifier",
                )
            )
            continue
        search_text = segment.search_text.strip()
        if not search_text:
            errors.append(f"youtube_identity_search_text_empty:{segment_path}")
            continue
        expected_youtube.add(
            (
                segment.segment_id,
                segment.parent_source_id,
                segment.video_id,
                segment.segment_index,
                segment.start_seconds,
                segment.end_seconds,
                segment.start_url,
                _normalise_index_path(str(segment_path)),
                segment.title or "YouTube",
                search_text,
            )
        )
    expected_sources = {
        (source_id, _normalise_index_path(values[0]) if values[0] else "", _normalise_index_path(values[1]) if values[1] else "", values[2])
        for source_id, values in expected_sources_by_id.items()
    }
    actual_fts = {
        (
            str(row[0]),
            _normalise_index_path(str(row[1])),
            _normalise_index_path(str(row[2])),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
        )
        for row in snapshot.get("fts", {}).get("cards", [])
        if isinstance(row, list) and len(row) == 9
    }
    actual_youtube = {
        (
            str(row[0]), str(row[1]), str(row[2]), int(row[3]), row[4], row[5],
            str(row[6]), _normalise_index_path(str(row[7])), str(row[8]), str(row[9]),
        )
        for row in snapshot.get("fts", {}).get("youtube", [])
        if isinstance(row, list) and len(row) == 10
    }
    actual_wiki = {
        (int(row[0]), str(row[1]), str(row[2]))
        for row in snapshot.get("fts", {}).get("wiki", [])
        if isinstance(row, list) and len(row) == 3
    }
    actual_wiki_documents = {
        (int(row[0]), str(row[4]), str(row[5]))
        for row in snapshot.get("fts", {}).get("wiki_documents", [])
        if isinstance(row, list) and len(row) == 9
    }
    # The projection table is the runner-owned source of truth for the Wiki
    # FTS virtual table.  Compare both directions and require the whole Wiki
    # projection to be empty when the feature is disabled; a mere ``> 0``
    # count is not an identity check.
    expected_wiki = actual_wiki_documents if config.WIKI_ENABLED else set()
    expected_wiki_documents = actual_wiki_documents if config.WIKI_ENABLED else set()
    actual_sources = {
        (str(row[0]), _normalise_index_path(str(row[1])) if row[1] else "", _normalise_index_path(str(row[2])) if row[2] else "", str(row[3]))
        for row in snapshot.get("registry", {}).get("sources", []) if isinstance(row, list) and len(row) == 4
    }
    actual_normalized = {
        (str(row[0]), _normalise_index_path(str(row[1])))
        for row in snapshot.get("registry", {}).get("normalized_docs", []) if isinstance(row, list) and len(row) == 2
    }
    actual_enriched = {
        (str(row[0]), _normalise_index_path(str(row[1])), str(row[2]))
        for row in snapshot.get("registry", {}).get("enriched_cards", []) if isinstance(row, list) and len(row) == 3
    }
    actual_references = [tuple(map(str, row)) for row in snapshot.get("registry", {}).get("references", []) if isinstance(row, list) and len(row) == 4]
    expected_reference_set = set(expected_references)
    comparisons = {
        "fts_cards": (expected_fts, actual_fts),
        "fts_youtube": (expected_youtube, actual_youtube),
        "fts_wiki": (expected_wiki, actual_wiki),
        "fts_wiki_documents": (expected_wiki_documents, actual_wiki_documents),
        "registry_sources": (expected_sources, actual_sources),
        "registry_normalized_docs": (expected_normalized, actual_normalized),
        "registry_enriched_cards": (expected_enriched, actual_enriched),
        "registry_references": (expected_reference_set, set(actual_references)),
    }
    for label, (expected, actual) in comparisons.items():
        if expected != actual:
            errors.append(f"{label}_identity_mismatch:missing={len(expected - actual)}:extra={len(actual - expected)}")
    if sorted(expected_references) != sorted(actual_references):
        errors.append(
            "registry_references_row_multiplicity_mismatch:"
            f"expected={len(expected_references)}:actual={len(actual_references)}"
        )
    actual_aliases = {str(row[0]) for row in snapshot.get("metadata_aliases", []) if isinstance(row, list) and row}
    canonical_paths = {
        _normalise_index_path(path)
        for path in expected_paths
        if "__geospoiler__" not in Path(str(path)).name
    }
    basename_counts: dict[str, int] = {}
    for path in canonical_paths:
        basename = Path(path).name
        basename_counts[basename] = basename_counts.get(basename, 0) + 1
    for path in canonical_paths:
        basename = Path(path).name
        if basename_counts[basename] == 1:
            expected_paths.add(basename)
    if not expected_paths.issubset(actual_aliases):
        errors.append(f"metadata_aliases_missing:{len(expected_paths - actual_aliases)}")
    if not actual_aliases.issubset(expected_paths):
        errors.append(f"metadata_aliases_extra:{len(actual_aliases - expected_paths)}")
    return not errors, errors


def _read_json(path: Path) -> Any:
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _validate_verification_publication_snapshot(
    snapshot: list[dict[str, Any]], expected_names: set[str]
) -> None:
    """Validate manifest hashes from the same immutable bytes accepted by scan."""
    with tempfile.TemporaryDirectory(prefix="release-verification-validation-") as raw:
        validation_dir = Path(raw)
        materialize_release_artifact_tree_snapshot(snapshot, validation_dir)
        manifest = _read_json(validation_dir / "verification_manifest.json")
        for key in ("json", "markdown"):
            record = manifest.get(key, {}) if isinstance(manifest, dict) else {}
            target = validation_dir / str(record.get("path") or "")
            if (
                not target.is_file()
                or record.get("bytes") != target.stat().st_size
                or record.get("sha256") != sha256_file(target)
            ):
                raise RuntimeError(f"verification manifest self-check failed: {key}")
        physical_names = {
            path.name for path in validation_dir.iterdir() if path.is_file()
        }
        if physical_names != expected_names:
            raise RuntimeError("verification publication physical inventory mismatch")


def _sha256(path: Path) -> str:
    return sha256_file(path)


def _state_inventory(paths: list[Path]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted({item.resolve() for item in paths if item.is_file()}, key=lambda value: value.as_posix().casefold()):
        relative = path.relative_to(PROJECT_ROOT.resolve()).as_posix()
        size = path.stat().st_size
        total_bytes += size
        records.append({"path": relative, "bytes": size, "sha256": sha256_file(path)})
    return {
        "files": len(records),
        "bytes": total_bytes,
        "sha256": sha256_bytes(canonical_json(records)),
        "records": records,
    }


def _zero_or_empty(value: Any) -> bool:
    """Accept the scanner's list-shaped zero fields and legacy integer zeros."""
    return value == 0 or value == [] or value == {}


def _protected_files_are_clean(value: Any) -> bool:
    """Allow physical protected files only when the scanner measured zero findings."""
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("path"), str) or not item["path"].strip():
            return False
        occurrences = item.get("occurrences")
        if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences != 0:
            return False
    return True


def _security_inventory_is_fresh(payload: dict[str, Any]) -> bool:
    expected = payload.get("inventory_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    try:
        current = security_release_scan.scan()
    except Exception:
        return False
    return (
        current.get("status") == "passed"
        and current.get("inventory_sha256") == expected
        and current.get("finding_files") == 0
        and current.get("occurrences") == 0
        and current.get("protected_occurrences") == 0
        and current.get("metadata_occurrences") == 0
        and _protected_files_are_clean(current.get("protected_files"))
        and current.get("unreadable_files") == 0
        and _zero_or_empty(current.get("tracked_protected_files"))
        and _zero_or_empty(current.get("staged_protected_files"))
        and _zero_or_empty(current.get("git_inspection_errors"))
    )


def _git_value(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _media_contract() -> dict[str, Any]:
    media_total = 0
    images = 0
    videos = 0
    visual_status: dict[str, int] = {}
    transcription_status: dict[str, int] = {}
    mismatches: list[dict[str, Any]] = []

    for meta_path in sorted(config.NORMALIZED_DIR.rglob("*.meta.json")):
        try:
            meta = NormalizedMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - release guard
            mismatches.append(
                {
                    "path": str(meta_path),
                    "error": safe_exception_token(
                        "media_meta_invalid",
                        exc,
                        component="release_verifier",
                    ),
                }
            )
            continue
        items = _media_entries(meta)
        if not items:
            continue
        relative = meta_path.relative_to(config.NORMALIZED_DIR)
        card_path = config.ENRICHED_DIR / relative.parent / (relative.name[:-10] + ".enriched.json")
        if not card_path.is_file():
            mismatches.append({"path": str(meta_path), "error": "missing_card"})
            continue
        try:
            card = EnrichedCardV2.model_validate_json(card_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - release guard
            mismatches.append(
                {
                    "path": str(card_path),
                    "error": safe_exception_token(
                        "media_card_invalid",
                        exc,
                        component="release_verifier",
                    ),
                }
            )
            continue
        mismatch = _media_contract_pair(meta, card, str(meta_path))
        if mismatch:
            mismatches.append(mismatch)
        for item in items:
            media_total += 1
            media_type = str(item.get("media_type") or "").lower()
            images += media_type == "image"
            videos += media_type == "video"
            visual = str(item.get("visual_status") or "none").lower()
            visual_status[visual] = visual_status.get(visual, 0) + 1
            default_transcription = "na" if media_type == "image" else "none"
            transcription = str(item.get("transcription_status") or default_transcription).lower()
            transcription_status[transcription] = transcription_status.get(transcription, 0) + 1
    return {
        "total": media_total,
        "images": images,
        "videos": videos,
        "visual_status": visual_status,
        "transcription_status": transcription_status,
        "card_sidecar_mismatches": len(mismatches),
        "mismatch_details": mismatches,
    }


def _media_contract_pair(meta: NormalizedMeta, card: EnrichedCardV2, path: str) -> dict[str, Any] | None:
    """Return both missing and stale pipeline-owned media diagnostics."""
    expected_flags = set(_media_quality_flags(meta))
    expected_issues = set(_media_quality_issues(meta))
    actual_flags = set(card.quality_flags or [])
    actual_issues = set(card.extraction_issues or [])
    # Every flag in this set is produced by pipeline code, never by the LLM.
    # A successful reprocessing must therefore remove any old value, not only
    # the currently expected ``transcript_unavailable`` flag.
    owned_flags = set(_PIPELINE_OWNED_QUALITY_FLAGS)
    owned_issues = {item for item in actual_issues if item.startswith("media:")}
    mismatch = {
        "path": path,
        "missing_flags": sorted(expected_flags - actual_flags),
        "missing_issues": sorted(expected_issues - actual_issues),
        "stale_flags": sorted((actual_flags & owned_flags) - expected_flags),
        "stale_issues": sorted(owned_issues - expected_issues),
    }
    return mismatch if any(mismatch[key] for key in ("missing_flags", "missing_issues", "stale_flags", "stale_issues")) else None


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        payload = _read_json(path)
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(
            f"{path}:"
            + safe_exception_token(
                "invalid_evidence",
                exc,
                component="release_verifier",
            )
        )
        return {}
    if not isinstance(payload, dict):
        errors.append(f"evidence_not_object:{path}")
        return {}
    return payload


def _evidence_bundle(root: Path, errors: list[str]) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = root / "evidence_manifest.json"
    if not manifest_path.is_file():
        errors.append(f"missing_evidence:{manifest_path}")
        return {}, {}
    bundle = _load_json(manifest_path, errors)
    if bundle.get("schema_version") != RELEASE_EVIDENCE_SCHEMA:
        errors.append("evidence_manifest_schema_invalid")
    identity = bundle.get("release_identity")
    if not isinstance(identity, dict):
        errors.append("evidence_manifest_identity_missing")
        identity = {}
    reports = bundle.get("reports")
    if not isinstance(reports, list) or not reports:
        errors.append("evidence_manifest_reports_missing")
        reports = []
    paths: dict[str, Path] = {}
    seen_paths: set[str] = set()
    for record in reports:
        if not isinstance(record, dict):
            errors.append("evidence_manifest_record_invalid")
            continue
        kind = str(record.get("kind") or "")
        relative = Path(str(record.get("path") or ""))
        if not kind or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            errors.append(f"evidence_manifest_path_invalid:{relative}")
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            errors.append(f"evidence_manifest_path_escape:{relative}")
            continue
        if kind in paths:
            errors.append(f"evidence_manifest_duplicate_kind:{kind}")
            continue
        if kind in {"evidence_manifest"}:
            errors.append(f"evidence_manifest_reserved_kind:{kind}")
            continue
        relative_key = relative.as_posix().casefold()
        if relative_key in seen_paths:
            errors.append(f"evidence_manifest_duplicate_path:{relative}")
            continue
        seen_paths.add(relative_key)
        if not path.is_file():
            errors.append(f"evidence_manifest_missing_file:{relative}")
            continue
        if path.stat().st_size != record.get("bytes") or _sha256(path) != record.get("sha256"):
            errors.append(f"evidence_manifest_hash_mismatch:{relative}")
            continue
        paths[kind] = path
    expected = {
        "accepted_manifest",
        "backup_source_manifest",
        "run_index",
        *RELEASE_REPORT_KINDS,
    }
    if not expected.issubset(paths):
        errors.append("evidence_manifest_required_kinds_missing:" + ",".join(sorted(expected - set(paths))))
    allowed_auxiliary = {
        "test_log_pytest",
        "test_log_ruff",
        "test_log_compileall",
        "test_log_diff_check",
        "test_summary",
        "test_runner_receipts",
        "test_runner_lifecycle",
        "smoke_attempt_ledger",
    }
    extras = set(paths) - expected
    if extras - allowed_auxiliary:
        errors.append("evidence_manifest_unknown_kind")
    physical_files: set[Path] = set()
    if root.is_dir():
        for physical in root.rglob("*"):
            if physical.is_symlink():
                errors.append(f"evidence_manifest_symlink:{physical}")
                continue
            if physical.is_file() and physical.name != manifest_path.name:
                try:
                    physical_files.add(physical.resolve().relative_to(root.resolve()))
                except ValueError:
                    errors.append(f"evidence_manifest_physical_escape:{physical}")
    declared_files = {path.resolve().relative_to(root.resolve()) for path in paths.values() if path.is_file()}
    if physical_files != declared_files:
        errors.append(
            "evidence_manifest_physical_inventory_mismatch:"
            f"extra={sorted(str(item) for item in physical_files - declared_files)}"
            f" missing={sorted(str(item) for item in declared_files - physical_files)}"
        )
    return identity, paths


def _report(
    paths: dict[str, Path],
    errors: list[str],
    kind: str,
    *,
    required: bool = True,
    release_report: bool = True,
) -> dict[str, Any]:
    path = paths.get(kind)
    if path is None:
        if required:
            errors.append(f"missing_evidence_kind:{kind}")
        return {}
    payload = _load_json(path, errors)
    if release_report:
        errors.extend(f"report_contract:{kind}:{item}" for item in validate_report_envelope(payload, kind=kind))
    return payload


def _backup_snapshot_is_fresh(
    payload: dict[str, Any], paths: dict[str, Path], errors: list[str]
) -> bool:
    frozen = paths.get("backup_source_manifest")
    manifest_value = payload.get("manifest")
    if frozen is None or not isinstance(manifest_value, str) or not manifest_value:
        errors.append("backup_snapshot_evidence_missing")
        return False
    expected_manifest_sha = payload.get("backup_manifest_sha256")
    if _sha256(frozen) != expected_manifest_sha:
        errors.append("backup_frozen_manifest_mismatch")
        return False
    try:
        current_manifest = Path(manifest_value)
        if _sha256(current_manifest) != expected_manifest_sha:
            errors.append("backup_source_manifest_stale")
            return False
        current = validate_backup_restore.validate(current_manifest)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        errors.append("backup_snapshot_revalidation_failed")
        return False
    fields = (
        "backup_manifest_sha256",
        "backup_inventory_sha256",
        "files_declared",
        "files_checked",
        "nested_paths",
        "canonical_mapping",
    )
    fresh = all(current.get(field) == payload.get(field) for field in fields)
    if not fresh:
        errors.append("backup_snapshot_stale")
    return fresh


def _portable_snapshot_is_fresh(payload: dict[str, Any], errors: list[str]) -> bool:
    try:
        current = migrate_portable_paths.scan()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        errors.append("portable_snapshot_revalidation_failed")
        return False
    fields = (
        "inventory_sha256",
        "files_scanned",
        "scanned_files",
        "changed_files",
        "field_changes",
        "unresolved",
        "status",
    )
    fresh = all(current.get(field) == payload.get(field) for field in fields)
    if not fresh:
        errors.append("portable_snapshot_stale")
    return fresh


def _test_evidence(
    payload: dict[str, Any],
    bundled_paths: dict[str, Path] | None = None,
    *,
    trusted_git_snapshot: dict[str, Any] | None = None,
    release_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not payload:
        return {"status": "not_verified", "reason": "test evidence file is missing"}
    status = str(payload.get("status") or "").casefold()
    release_payload = payload.get("release_schema_version") == RELEASE_EVIDENCE_SCHEMA
    summary = payload.get("summary")
    if status not in {"passed", "pass", "ok"} or not isinstance(summary, dict):
        return {**payload, "status": "failed"}
    checks = payload.get("checks")
    required_checks = {"pytest", "ruff", "compileall", "diff_check"}
    if (
        not isinstance(checks, list)
        or len(checks) != len(required_checks)
        or {item.get("name") for item in checks if isinstance(item, dict)} != required_checks
    ):
        return {**payload, "status": "failed", "reason": "test checks are incomplete"}
    summary_path = bundled_paths.get("test_summary") if bundled_paths else None
    if summary_path is None and payload.get("summary_path"):
        summary_path = Path(str(payload["summary_path"]))
    if summary_path is None or not summary_path.is_file():
        return {**payload, "status": "failed", "reason": "test summary missing"}
    if payload.get("summary_sha256") != _sha256(summary_path):
        return {**payload, "status": "failed", "reason": "test summary hash mismatch"}
    try:
        bundled_summary = _read_json(summary_path)
    except (OSError, UnicodeError, ValueError):
        return {**payload, "status": "failed", "reason": "test summary unreadable"}
    if bundled_summary != summary:
        return {**payload, "status": "failed", "reason": "test summary content mismatch"}
    if release_payload:
        environment = summary.get("environment")
        if static_environment_receipt_errors(environment):
            return {**payload, "status": "failed", "reason": "static environment receipt failed or missing"}
    runner_receipts_path = bundled_paths.get("test_runner_receipts") if bundled_paths else None
    runner_lifecycle_path = bundled_paths.get("test_runner_lifecycle") if bundled_paths else None
    runner_receipts: dict[str, Any] = {}
    runner_lifecycle: dict[str, Any] = {}
    if release_payload:
        if payload.get("execution_mode") != "runner_receipt":
            return {**payload, "status": "failed", "reason": "runner execution mode missing"}
        if runner_receipts_path is None:
            runner_receipts_path = Path(str(payload.get("runner_receipts_path") or ""))
        if runner_lifecycle_path is None:
            runner_lifecycle_path = Path(str(payload.get("runner_lifecycle_path") or ""))
        if (
            runner_receipts_path is None
            or not runner_receipts_path.is_file()
            or payload.get("runner_receipts_sha256") != _sha256(runner_receipts_path)
            or runner_lifecycle_path is None
            or not runner_lifecycle_path.is_file()
            or payload.get("runner_lifecycle_sha256") != _sha256(runner_lifecycle_path)
        ):
            return {**payload, "status": "failed", "reason": "runner receipt missing or hash mismatch"}
        try:
            runner_receipts = _read_json(runner_receipts_path)
            runner_lifecycle = _read_json(runner_lifecycle_path)
        except (OSError, UnicodeError, ValueError):
            return {**payload, "status": "failed", "reason": "runner receipt unreadable"}
        if (
            runner_receipts.get("schema_version") != "release_test_runner_receipts_v1"
            or not isinstance(runner_receipts.get("gates"), dict)
            or runner_lifecycle.get("schema_version") != "release_test_runner_lifecycle_v1"
            or runner_lifecycle.get("status") != payload.get("status")
        ):
            return {**payload, "status": "failed", "reason": "runner lifecycle contract invalid"}
        try:
            lifecycle_finished_at = datetime.fromisoformat(runner_lifecycle["finished_at"])
        except ValueError:
            return {**payload, "status": "failed", "reason": "runner lifecycle contract invalid"}
        if lifecycle_finished_at.tzinfo is None:
            return {**payload, "status": "failed", "reason": "runner lifecycle contract invalid"}
    expected_argv: dict[str, Any] = {
        "pytest": [sys.executable, "-m", "pytest", "-q"],
        "ruff": [sys.executable, "-m", "ruff", "check", "."],
        "compileall": [sys.executable, "-m", "compileall", "-q", "."],
        "diff_check": None,
    }
    if release_payload:
        try:
            git_snapshot = trusted_git_snapshot or release_git_snapshot(TRUSTED_RELEASE_BASE_REF)
        except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError):
            return {**payload, "status": "failed", "reason": "trusted git identity unavailable"}
        identity = release_identity if isinstance(release_identity, dict) else payload.get("release_identity")
        if not isinstance(identity, dict):
            return {**payload, "status": "failed", "reason": "release identity missing"}
        expected_fields = {
            "base_ref": git_snapshot.get("base_ref"),
            "base_commit": git_snapshot.get("base_commit"),
            "merge_base": git_snapshot.get("merge_base"),
            "head_commit": git_snapshot.get("head_commit"),
            "diff_check_argv": git_snapshot.get("diff_check_argv"),
        }
        if any(payload.get(field) != expected for field, expected in expected_fields.items()):
            return {**payload, "status": "failed", "reason": "base-aware test identity mismatch"}
        if identity.get("git_commit") != git_snapshot.get("head_commit"):
            return {**payload, "status": "failed", "reason": "test HEAD does not match release identity"}
        expected_argv["diff_check"] = list(git_snapshot["diff_check_argv"])
        if (
            runner_lifecycle.get("base_ref") != git_snapshot.get("base_ref")
            or runner_lifecycle.get("commands") != expected_argv
            or runner_lifecycle.get("completed_gates") != list(expected_argv)
            or runner_lifecycle.get("summary_path") != payload.get("summary_path")
            or runner_lifecycle.get("receipts_path") != payload.get("runner_receipts_path")
            or not isinstance(runner_lifecycle.get("finished_at"), str)
        ):
            return {**payload, "status": "failed", "reason": "runner lifecycle contract invalid"}
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "passed" or check.get("exit_code") != 0:
            return {**payload, "status": "failed", "reason": "test check failed"}
        log_path = bundled_paths.get(f"test_log_{check.get('name')}") if bundled_paths else None
        if log_path is None:
            log_path = Path(str(check.get("log_path") or ""))
        log_sha = check.get("log_sha256")
        if (
            not log_path.is_file()
            or not isinstance(log_sha, str)
            or _sha256(log_path) != log_sha
            or log_path.stat().st_size != check.get("log_bytes")
            or not isinstance(check.get("command_argv"), list)
            or not check.get("command_argv")
            or not isinstance(check.get("cwd"), str)
            or not check.get("started_at")
            or not check.get("finished_at")
            or not _is_duration_ms(check.get("duration_ms"))
            or (release_payload and check.get("timed_out") is not False)
            or (release_payload and check.get("interrupted") is not False)
        ):
            return {**payload, "status": "failed", "reason": "test log missing or hash mismatch"}
        expected = expected_argv.get(str(check.get("name")))
        if release_payload:
            if not isinstance(expected, list) or check.get("command_argv") != expected:
                return {**payload, "status": "failed", "reason": "test command receipt invalid"}
            try:
                cwd_matches = Path(check["cwd"]).resolve(strict=False) == Path(str(git_snapshot["project_root"])).resolve(strict=False)
                started_at = datetime.fromisoformat(str(check["started_at"]))
                finished_at = datetime.fromisoformat(str(check["finished_at"]))
                elapsed_ms = (finished_at - started_at).total_seconds() * 1000.0
            except (KeyError, OSError, TypeError, ValueError):
                return {**payload, "status": "failed", "reason": "test timing or cwd invalid"}
            duration_ms = float(check["duration_ms"])
            if (
                not cwd_matches
                or started_at.tzinfo is None
                or finished_at.tzinfo is None
                or elapsed_ms < 0
                or abs(duration_ms - elapsed_ms) > max(1000.0, elapsed_ms * 0.05)
                or check.get("timeout_seconds") != RELEASE_TEST_TIMEOUT_SECONDS
                or not isinstance(check.get("timed_out"), bool)
                or not isinstance(check.get("interrupted"), bool)
            ):
                return {**payload, "status": "failed", "reason": "test timing or cwd invalid"}
            receipt = runner_receipts.get("gates", {}).get(str(check.get("name")))
            if (
                not isinstance(receipt, dict)
                or receipt.get("exit_code") != check.get("exit_code")
                or receipt.get("command_argv") != check.get("command_argv")
                or receipt.get("cwd") != check.get("cwd")
                or receipt.get("started_at") != check.get("started_at")
                or receipt.get("finished_at") != check.get("finished_at")
                or receipt.get("duration_ms") != check.get("duration_ms")
                or receipt.get("timeout_seconds") != check.get("timeout_seconds")
                or receipt.get("timed_out") != check.get("timed_out")
                or receipt.get("interrupted") != check.get("interrupted")
                or receipt.get("log_bytes") != check.get("log_bytes")
                or receipt.get("log_sha256") != check.get("log_sha256")
            ):
                return {**payload, "status": "failed", "reason": "runner receipt does not match check"}
        else:
            expected_tokens = {
                "pytest": {"pytest"},
                "ruff": {"ruff"},
                "compileall": {"compileall"},
                "diff_check": {"diff", "--check"},
            }.get(str(check.get("name")), set())
            argv_text = " ".join(str(item) for item in check.get("command_argv", []))
            if not expected_tokens or not all(token in argv_text for token in expected_tokens):
                return {**payload, "status": "failed", "reason": "test command receipt invalid"}
    if release_payload and (summary.get("gates_failed") != 0 or summary.get("timeouts") != 0):
        return {**payload, "status": "failed", "reason": "test summary reports failed gate"}
    if release_payload:
        counts = summary.get("pytest_counts")
        if (
            not isinstance(counts, dict)
            or counts.get("parsed") is not True
            or not isinstance(counts.get("passed"), int)
            or counts.get("passed", 0) <= 0
            or counts.get("failed") != 0
        ):
            return {**payload, "status": "failed", "reason": "pytest counts are missing or failed"}
    return {**payload, "status": "passed"}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _wiki_fts_gate(fts: dict[str, Any]) -> bool:
    """Require an exact Wiki FTS/projection pair under the active flag."""
    wiki_rows = fts.get("wiki_fts", 0)
    projection_rows = fts.get("wiki_documents_fts", 0)
    if config.WIKI_ENABLED:
        return wiki_rows == projection_rows and wiki_rows > 0
    return wiki_rows == 0 and projection_rows == 0


def _manifest_sources(
    manifest: dict[str, Any],
    errors: list[str],
    *,
    validation: Any | None = None,
) -> list[dict[str, Any]]:
    if validation is None:
        validation = validate_manifest_payload(manifest)
        errors.extend(f"manifest_contract:{item}" for item in validation.errors)
    return list(validation.entries)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_duration_ms(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _cold_runtime_is_strict(payload: dict[str, Any]) -> bool:
    """Reject cold evidence with missing or hidden runtime/timeout overrides."""
    effective = payload.get("effective_cold_runtime")
    current = runtime_identity()
    expected = current.get("cold_graph")
    if not isinstance(effective, dict) or not isinstance(expected, dict):
        return False
    if payload.get("batch_size") != effective.get("batch_size"):
        return False
    if payload.get("batch_timeout_seconds") != effective.get("batch_timeout_seconds"):
        return False
    if payload.get("config_identity_sha256") != effective.get("config_identity_sha256"):
        return False
    if effective.get("batch_size") != expected.get("batch_size_default"):
        return False
    if effective.get("batch_timeout_seconds") != expected.get("batch_timeout_seconds_default"):
        return False
    if effective.get("batch_size_override") is not False:
        return False
    if effective.get("batch_timeout_override") is not False:
        return False
    if effective.get("override") is not False:
        return False
    if effective.get("batch_size_max") != expected.get("batch_size_max"):
        return False
    if effective.get("batch_timeout_seconds_default") != expected.get("batch_timeout_seconds_default"):
        return False
    if effective.get("rag_runtime") != current.get("rag_runtime"):
        return False
    if effective.get("config_identity_sha256") != config_identity_sha256():
        return False
    return True


def _cold_lineage_is_strict(attempt: dict[str, Any], expected_source_ids: set[str]) -> bool:
    """Require disjoint lineage sets that exactly cover processed sources."""
    if "inserted" in attempt:
        return False
    lineage = attempt.get("lineage")
    if not isinstance(lineage, dict):
        return False
    names = (
        "inserted_source_ids",
        "reused_processed_source_ids",
        "processed_terminal_source_ids",
    )
    sets: dict[str, set[str]] = {}
    for name in names:
        values = lineage.get(name)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            return False
        value_set = set(values)
        if len(value_set) != len(values) or not value_set.issubset(expected_source_ids):
            return False
        sets[name] = value_set
    if sets["inserted_source_ids"] & sets["reused_processed_source_ids"]:
        return False
    if sets["processed_terminal_source_ids"] != expected_source_ids:
        return False
    if sets["inserted_source_ids"] | sets["reused_processed_source_ids"] != expected_source_ids:
        return False
    return (
        attempt.get("inserted_by_loader") == len(sets["inserted_source_ids"])
        and attempt.get("reused_processed") == len(sets["reused_processed_source_ids"])
        and attempt.get("processed_terminal") == len(expected_source_ids)
    )


def _cold_provenance_is_strict(payload: dict[str, Any], manifest_sources: list[dict[str, Any]]) -> bool:
    """Recompute source/card/graph and current cold storage proofs at verdict time."""
    try:
        source_proof = compute_source_proof({"sources": manifest_sources})
        reported_source_proof = payload.get("source_proof_before_load")
        if reported_source_proof != source_proof:
            return False
        if payload.get("source_proof_sha256") != source_proof.get("source_proof_sha256"):
            return False
        expected_source_ids = set(source_proof.get("source_ids") or [])
        expected_doc_ids = set(source_proof.get("doc_ids") or [])
        if payload.get("source_ids") != sorted(expected_source_ids):
            return False
        if payload.get("doc_ids") != sorted(expected_doc_ids):
            return False
        candidate_value = payload.get("candidate_path")
        workspace_value = payload.get("workspace")
        if not isinstance(candidate_value, str) or not isinstance(workspace_value, str):
            return False
        workspace = Path(workspace_value).resolve(strict=False)
        candidate_path = Path(candidate_value)
        candidate = candidate_path.resolve(strict=False)
        if candidate != (workspace / "rag_storage").resolve(strict=False):
            return False
        loaded = loaded_doc_proof(source_proof, candidate)
        if payload.get("loaded_doc_proof") != loaded:
            return False
        current_inventory = storage_inventory(candidate_path)
        if payload.get("storage_inventory") != current_inventory or current_inventory.get("checked") is not True:
            return False
        attempt = payload.get("cold_graph_attempt")
        if not isinstance(attempt, dict) or not _cold_lineage_is_strict(attempt, expected_source_ids):
            return False
        if attempt.get("source_ids") != sorted(expected_source_ids) or attempt.get("doc_ids") != sorted(expected_doc_ids):
            return False
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError, UnicodeError):
        return False
    return True


def _cold_graph_is_strict(payload: dict[str, Any], manifest_sources: list[dict[str, Any]]) -> bool:
    """Require immutable per-source terminal evidence for a cold graph run."""
    if "cold_runner_override" in payload:
        return False
    effective = payload.get("effective_runtime_identity")
    release_identity = payload.get("release_identity")
    if not isinstance(effective, dict) or not isinstance(release_identity, dict):
        return False
    if effective != release_identity.get("runtime_identity"):
        return False
    attempt = payload.get("cold_graph_attempt")
    if not isinstance(attempt, dict):
        return False
    records = attempt.get("sources")
    if not isinstance(records, list) or len(records) != len(manifest_sources):
        return False
    status_counts = attempt.get("status_counts")
    if status_counts is not None and not isinstance(status_counts, dict):
        return False
    if not _cold_runtime_is_strict(payload):
        return False
    if not _cold_provenance_is_strict(payload, manifest_sources):
        return False
    inventory = payload.get("workspace_inventory")
    if (
        payload.get("source_storage_copied") is not False
        or not isinstance(inventory, dict)
        or inventory.get("checked") is not True
        or inventory.get("source_storage_copied") is not False
    ):
        return False
    for field in ("inserted_by_loader", "reused_processed", "processed_terminal"):
        value = attempt.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    if attempt.get("processed_terminal") != attempt.get("processed") or attempt.get(
        "inserted_by_loader", 0
    ) + attempt.get("reused_processed", 0) != attempt.get("processed_terminal"):
        return False
    for field in ("processing", "failed", "unknown"):
        value = attempt.get(field, status_counts.get(field, 0) if isinstance(status_counts, dict) else 0)
        if not isinstance(value, int) or isinstance(value, bool) or value != 0:
            return False
    expected_by_id: dict[str, dict[str, Any]] = {}
    for source in manifest_sources:
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id in expected_by_id:
            return False
        expected_by_id[source_id] = source
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            return False
        source_id = str(record.get("source_id") or "")
        if source_id in seen or source_id not in expected_by_id:
            return False
        seen.add(source_id)
        if record.get("status") != "processed" or record.get("terminal_status") != "processed":
            return False
        if record.get("error") not in (None, ""):
            return False
        if not _is_duration_ms(record.get("duration_ms")):
            return False
        expected = expected_by_id[source_id]
        if record.get("normalized_sha256") != expected.get("normalized_sha256"):
            return False
        if record.get("enriched_sha256") != expected.get("enriched_sha256"):
            return False
        if record.get("graph_text_sha256") != expected.get("graph_text_sha256"):
            return False
        if (
            not _is_sha256(record.get("normalized_sha256"))
            or not _is_sha256(record.get("enriched_sha256"))
            or not _is_sha256(record.get("graph_text_sha256"))
        ):
            return False
        if record.get("measured_normalized_sha256") != expected.get("normalized_sha256"):
            return False
        if record.get("measured_enriched_sha256") != expected.get("enriched_sha256"):
            return False
        if record.get("measured_graph_text_sha256") != expected.get("graph_text_sha256"):
            return False
        loaded_hash = record.get("loaded_full_doc_sha256")
        load_expected = record.get("graph_text_for_load_sha256")
        if not _is_sha256(loaded_hash) or loaded_hash != load_expected or record.get("loaded_full_doc_matches") is not True:
            return False
    source_ids = {str(record.get("source_id") or "") for record in records}
    if source_ids != set(expected_by_id):
        return False
    storage_inventory = payload.get("storage_inventory")
    if (
        not isinstance(storage_inventory, dict)
        or storage_inventory.get("checked") is not True
        or not isinstance(storage_inventory.get("inventory_sha256"), str)
        or not isinstance(storage_inventory.get("records"), list)
    ):
        return False
    return len(seen) == len(expected_by_id)


def _smoke_is_strict(payload: dict[str, Any]) -> bool:
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != len(OFFICIAL_SMOKE_CASES):
        return False
    attempts = payload.get("attempts")
    attempt_id = payload.get("attempt_id")
    if (
        not isinstance(attempts, list)
        or not attempts
        or not smoke_attempt_chain_is_valid(attempts)
        or not isinstance(attempt_id, str)
        or not any(isinstance(item, dict) and item.get("attempt_id") == attempt_id for item in attempts)
    ):
        return False
    if attempts[-1].get("attempt_id") != attempt_id:
        return False
    latest = attempts[-1]
    cases_sha = sha256_bytes(canonical_json(cases))
    if (
        latest.get("passed") is not True
        or latest.get("case_count") != len(cases)
        or not isinstance(latest.get("errors"), list)
        or latest.get("errors") != []
        or latest.get("cases_sha256") != cases_sha
        or payload.get("cases_sha256") != cases_sha
    ):
        return False
    attempt_ids = [item.get("attempt_id") for item in attempts if isinstance(item, dict)]
    if len(attempt_ids) != len(set(attempt_ids)) or any(
        not isinstance(value, str) or not re.fullmatch(r"attempt-\d{3}", value) for value in attempt_ids
    ):
        return False
    expected_cases = [
        {"id": case_id, "question": question, "profile": profile}
        for case_id, question, profile in OFFICIAL_SMOKE_CASES
    ]
    if payload.get("official_cases_sha256") != OFFICIAL_CASES_SHA256:
        return False
    actual_specs = [
        {"id": case.get("id"), "question": case.get("question"), "profile": case.get("profile")}
        for case in cases if isinstance(case, dict)
    ]
    if actual_specs != expected_cases:
        return False
    for case in cases:
        if not isinstance(case, dict):
            return False
        if case.get("pipeline") != "late_fusion":
            return False
        if case.get("fallback") is not False:
            return False
        if not isinstance(case.get("errors"), list) or case["errors"]:
            return False
        if not isinstance(case.get("citation_count"), int) or case["citation_count"] < 1:
            return False
        if not isinstance(case.get("reference_count"), int) or case["reference_count"] < 1:
            return False
        if not isinstance(case.get("youtube_start_url_count"), int) or case["youtube_start_url_count"] < 0:
            return False
        references = case.get("references")
        citation_ids = case.get("citation_ids")
        if not isinstance(references, list) or not isinstance(citation_ids, list):
            return False
        reference_ids = [str(item.get("reference_id") or "") for item in references if isinstance(item, dict)]
        if (
            case.get("reference_count") != len(references)
            or case.get("citation_count") != len(set(citation_ids))
            or len(reference_ids) != len(set(reference_ids))
            or not reference_ids
            or any(
            not isinstance(item, dict)
            or not re.fullmatch(r"S\d+", str(item.get("reference_id") or ""))
            or not re.match(r"^https?://", str(item.get("url") or ""), re.IGNORECASE)
            or re.search(r"(?i)(?:access[-_]?token|api[-_]?key|authorization)\s*=\s*(?!%5bredacted%5d|\[redacted\])[^&\s]+", str(item.get("url") or ""))
            or (item.get("start_url") is not None and not re.match(r"^https?://", str(item.get("start_url") or ""), re.IGNORECASE))
            for item in references
            )
        ):
            return False
        reference_id_set = set(reference_ids)
        if any(str(value) not in reference_id_set for value in citation_ids):
            return False
        if case.get("youtube_start_url_count") != sum(bool(item.get("start_url")) for item in references):
            return False
    return True


def _smoke_ledger_is_strict(payload: dict[str, Any], ledger_path: Path | None) -> bool:
    """Require the bundled raw attempt ledger to equal the stamped attempts."""
    if ledger_path is None or not ledger_path.is_file():
        return False
    try:
        ledger = _read_json(ledger_path)
    except (OSError, UnicodeError, ValueError):
        return False
    return smoke_attempt_chain_is_valid(ledger) and ledger == payload.get("attempts")


def build_report(
    artifact_dir: Path,
    expected_release_run_id: str | None = None,
    *,
    trusted_base_ref: str = TRUSTED_RELEASE_BASE_REF,
) -> dict[str, Any]:
    errors: list[str] = []
    artifact_dir = artifact_dir.resolve()
    artifact_scan = scan_release_artifact_tree(artifact_dir)
    if artifact_scan.get("status") != "passed":
        errors.append("artifact_secret_scan_failed")
    try:
        trusted_git = release_git_snapshot(trusted_base_ref)
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError):
        trusted_git = {}
        errors.append("trusted_git_snapshot_failed")
    bundle_identity, evidence_paths = _evidence_bundle(artifact_dir, errors)
    manifest_path = evidence_paths.get("accepted_manifest")
    manifest_validation = None
    if manifest_path is None:
        errors.append("missing_evidence:accepted_manifest")
        manifest = {}
    else:
        manifest_validation = load_manifest_file(manifest_path)
        errors.extend(f"manifest_contract:{item}" for item in manifest_validation.errors)
        manifest = manifest_validation.payload
    run_index = _report(evidence_paths, errors, "run_index", required=True, release_report=False)
    if run_index:
        if run_index.get("schema_version") != "release_run_index_v1":
            errors.append("run_index_schema_invalid")
        if run_index.get("release_run_id") != bundle_identity.get("release_run_id"):
            errors.append("run_index_release_run_id_mismatch")
        if run_index.get("manifest_sha256") != bundle_identity.get("manifest_sha256"):
            errors.append("run_index_manifest_sha_mismatch")
        if run_index.get("bundle_path") != ".":
            errors.append("run_index_bundle_path_not_self_contained")
        if not re.fullmatch(r"attempt-\d{3}", str(run_index.get("attempt_name") or "")):
            errors.append("run_index_attempt_name_invalid")
        if run_index.get("report_kinds") != sorted(RELEASE_REPORT_KINDS):
            errors.append("run_index_report_kinds_mismatch")
    load_report = _report(evidence_paths, errors, "manifest_load")
    provenance = _report(evidence_paths, errors, "provenance")
    security = _report(evidence_paths, errors, "security")
    backup = _report(evidence_paths, errors, "backup")
    portable = _report(evidence_paths, errors, "portable_paths")
    backup_snapshot_fresh = _backup_snapshot_is_fresh(backup, evidence_paths, errors)
    portable_snapshot_fresh = _portable_snapshot_is_fresh(portable, errors)
    activation = _report(evidence_paths, errors, "cold_graph")
    smoke = _report(evidence_paths, errors, "late_fusion_smoke")
    tests_payload = _report(evidence_paths, errors, "tests")
    risk_register = _report(evidence_paths, errors, "risk_register")
    try:
        validated_risks = validate_risk_register_payload({"risks": risk_register.get("risks")})
    except (TypeError, ValueError):
        validated_risks = []
        errors.append("risk_register_contract_invalid")
    token_risks = [entry for entry in validated_risks if entry.get("id") == "RISK-TOKEN-ROTATION-001"]
    risk = token_risks[0] if len(token_risks) == 1 else {}
    security_inventory_fresh = _security_inventory_is_fresh(security)
    manifest_sources = _manifest_sources(manifest, errors, validation=manifest_validation)
    manifest_identity_run_id = expected_release_run_id or bundle_identity.get("release_run_id")
    try:
        validate_manifest_run_id(manifest_identity_run_id, manifest)
    except ValueError as exc:
        errors.append(
            safe_exception_token(
                "manifest_run_id_preflight",
                exc,
                component="release_verifier",
            )
        )

    # Every report is bound to one immutable identity. A report that only says
    # {"status":"passed"} is deliberately insufficient for release approval.
    required_identity_fields = {
        "release_run_id",
        "git_commit",
        "git_tree_sha256",
        "working_tree_sha256",
        "config_identity_sha256",
        "runtime_identity",
        "manifest_path",
        "manifest_sha256",
        "corpus_identity_sha256",
    }
    if not required_identity_fields.issubset(bundle_identity):
        errors.append("release_identity_incomplete")
    else:
        try:
            if bundle_identity["git_commit"] != git_commit():
                errors.append("release_identity_git_commit_mismatch")
            if bundle_identity["git_tree_sha256"] != git_tree_sha256():
                errors.append("release_identity_git_tree_mismatch")
            if bundle_identity["working_tree_sha256"] != working_tree_sha256():
                errors.append("release_identity_working_tree_mismatch")
            if bundle_identity["config_identity_sha256"] != config_identity_sha256():
                errors.append("release_identity_config_mismatch")
            if bundle_identity["runtime_identity"] != runtime_identity():
                errors.append("release_identity_runtime_config_mismatch")
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            errors.append(
                safe_exception_token(
                    "release_identity_runtime_error",
                    exc,
                    component="release_verifier",
                )
            )
    for kind, path in evidence_paths.items():
        if kind in {"accepted_manifest", "run_index"}:
            continue
        payload = _load_json(path, errors)
        if payload.get("release_identity") != bundle_identity:
            errors.append(f"report_identity_mismatch:{kind}")

    manifest_path = evidence_paths.get("accepted_manifest", artifact_dir / "accepted_manifest.json")
    manifest_sha = _sha256(manifest_path) if manifest_path.is_file() else None
    if bundle_identity.get("manifest_sha256") != manifest_sha:
        errors.append("release_identity_manifest_sha_mismatch")
    if manifest.get("run_id") != bundle_identity.get("release_run_id"):
        errors.append("manifest_release_run_id_mismatch")

    expected_corpus_sha = None
    try:
        from geospoiler.operations.release_identity import corpus_identity_sha256

        expected_corpus_sha = corpus_identity_sha256(manifest)
    except (TypeError, ValueError, KeyError) as exc:
        errors.append(
            safe_exception_token(
                "corpus_identity_recompute_failed",
                exc,
                component="release_verifier",
            )
        )
    if bundle_identity.get("corpus_identity_sha256") != expected_corpus_sha:
        errors.append("release_identity_corpus_mismatch")
    if not manifest_sources:
        errors.append("manifest_has_no_release_sources")

    # Re-measure the current corpus independently from the producer reports.
    # This is the freshness gate: an ignored file or card changed after the
    # manifest was produced must invalidate the release even if counts remain.
    try:
        current_source_proof = compute_source_proof(manifest)
        current_source_records = {
            str(record.get("source_id")): record
            for record in current_source_proof.get("records", [])
            if isinstance(record, dict)
        }
        expected_source_records = {
            str(entry.get("source_id")): entry for entry in manifest_sources
        }
        manifest_files_fresh = (
            set(current_source_records) == set(expected_source_records)
            and all(
                current_source_records[source_id].get(field) == expected_source_records[source_id].get(field)
                for source_id in expected_source_records
                for field in ("normalized_sha256", "enriched_sha256", "graph_text_sha256")
            )
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        current_source_proof = {}
        manifest_files_fresh = False
        errors.append(
            safe_exception_token(
                "current_source_proof_failed",
                exc,
                component="release_verifier",
            )
        )

    normalized_txt = sorted(config.NORMALIZED_DIR.rglob("*.txt"))
    normalized_meta = sorted(config.NORMALIZED_DIR.rglob("*.meta.json"))
    enriched_cards = sorted(config.ENRICHED_DIR.rglob("*.enriched.json"))
    youtube_segment_paths = sorted(config.YOUTUBE_SEGMENTS_DIR.rglob("*.youtube-segment.json"))
    try:
        status = _read_json(config.RAG_STORAGE_DIR / "kv_store_doc_status.json")
        full_docs = _read_json(config.RAG_STORAGE_DIR / "kv_store_full_docs.json")
        full_entities = _read_json(config.RAG_STORAGE_DIR / "kv_store_full_entities.json")
        full_relations = _read_json(config.RAG_STORAGE_DIR / "kv_store_full_relations.json")
        text_chunks = _read_json(config.RAG_STORAGE_DIR / "kv_store_text_chunks.json")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(
            safe_exception_token(
                "rag_storage_invalid",
                exc,
                component="release_verifier",
            )
        )
        status = full_docs = full_entities = full_relations = text_chunks = {}
    status_counts: dict[str, int] = {}
    for record in status.values():
        value = str(record.get("status") or "unknown").lower().rsplit(".", 1)[-1]
        status_counts[value] = status_counts.get(value, 0) + 1
    expected_doc_ids = set(current_source_proof.get("doc_ids") or [])
    expected_source_ids = set(current_source_proof.get("source_ids") or [])
    graph_exact_sets = (
        bool(expected_doc_ids)
        and set(status) == expected_doc_ids
        and set(full_docs) == expected_doc_ids
        and all(str(record.get("status") or "").casefold().rsplit(".", 1)[-1] == "processed" for record in status.values() if isinstance(record, dict))
    )
    provenance_source_ids = set(provenance.get("source_ids") or []) if isinstance(provenance, dict) else set()
    provenance_doc_ids = set(provenance.get("doc_ids") or []) if isinstance(provenance, dict) else set()
    provenance_exact_sets = (
        provenance_source_ids == expected_source_ids
        and provenance_doc_ids == expected_doc_ids
    )
    expected_manifest_source_ids = {str(entry.get("source_id")) for entry in manifest_sources}

    flags: dict[str, int] = {}
    marker_count = 0
    markers = ("[Транскрипция аудио]", "[Текст с экрана (OCR)]", "[Instagram Reel:")
    for card_path in enriched_cards:
        card = _read_json(card_path)
        for flag in card.get("quality_flags") or []:
            flags[flag] = flags.get(flag, 0) + 1
        semantic_text = json.dumps(
            tuple(
                card.get(field)
                for field in (
                    "summary",
                    "key_points",
                    "entities",
                    "topics",
                    "theses",
                    "quotes",
                    "events",
                    "search_phrases",
                    "graph_text",
                    "search_text",
                )
            ),
            ensure_ascii=False,
        )
        marker_count += sum(marker in semantic_text for marker in markers)

    fts = {
        "cards_fts": 0,
        "youtube_segments_fts": 0,
        "wiki_fts": 0,
        "wiki_documents_fts": 0,
    }
    registry = {
        "sources": 0,
        "normalized_docs": 0,
        "enriched_cards": 0,
        "references": 0,
        "references_with_urls": 0,
    }
    rag_state_files = sorted(
        path
        for path in config.RAG_STORAGE_DIR.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() not in {".sqlite", ".db"}
        and not path.name.casefold().endswith(("-wal", "-shm", "-journal"))
    )
    state_inventory = _state_inventory(normalized_txt + normalized_meta + enriched_cards + rag_state_files)
    state_inventory["manifest_sha256"] = manifest_sha
    state_inventory["corpus_identity_sha256"] = bundle_identity.get("corpus_identity_sha256")
    current_reconciliation: dict[str, Any] = {}
    current_snapshot: dict[str, Any] = {}
    provenance_fresh = False
    state_snapshot_match = False
    fts_registry_exact = False
    exact_index_errors: list[str] = []
    try:
        current_reconciliation = reconcile_current()
        current_snapshot = current_reconciliation.get("state_snapshot")
        if not isinstance(current_snapshot, dict):
            current_snapshot = _state_snapshot()
        fts_snapshot = current_snapshot.get("fts", {})
        fts = {
            "cards_fts": len(fts_snapshot.get("cards", [])),
            "youtube_segments_fts": len(fts_snapshot.get("youtube", [])),
            "wiki_fts": len(fts_snapshot.get("wiki", [])),
            "wiki_documents_fts": len(fts_snapshot.get("wiki_documents", [])),
        }
        registry_snapshot = current_snapshot.get("registry", {})
        references = registry_snapshot.get("references", [])
        registry = {
            "sources": len(registry_snapshot.get("sources", [])),
            "normalized_docs": len(registry_snapshot.get("normalized_docs", [])),
            "enriched_cards": len(registry_snapshot.get("enriched_cards", [])),
            "references": len(references),
            "references_with_urls": sum(
                isinstance(row, list)
                and len(row) == 4
                and isinstance(row[2], str)
                and bool(row[2].strip())
                for row in references
            ),
        }
        producer_items = [
            {key: item.get(key) for key in ("doc_id", "file_path", "source_id", "classification")}
            for item in provenance.get("items", []) if isinstance(item, dict)
        ]
        current_items = [
            {key: item.get(key) for key in ("doc_id", "file_path", "source_id", "classification")}
            for item in current_reconciliation.get("items", []) if isinstance(item, dict)
        ]
        provenance_fresh = (
            current_items == producer_items
            and current_reconciliation.get("source_ids") == provenance.get("source_ids")
            and current_reconciliation.get("doc_ids") == provenance.get("doc_ids")
            and current_reconciliation.get("counts") == provenance.get("counts")
        )
        state_snapshot_match = (
            isinstance(provenance.get("state_snapshot"), dict)
            and current_snapshot == provenance.get("state_snapshot")
        )
        registry_snapshot = current_snapshot.get("registry", {})
        fts_snapshot = current_snapshot.get("fts", {})
        registry_source_ids = {str(row[0]) for row in registry_snapshot.get("sources", []) if row}
        registry_normalized_ids = {str(row[0]) for row in registry_snapshot.get("normalized_docs", []) if row}
        registry_enriched_ids = {str(row[0]) for row in registry_snapshot.get("enriched_cards", []) if row}
        fts_card_ids = {str(row[0]) for row in fts_snapshot.get("cards", []) if row}
        fts_registry_exact = (
            registry_source_ids == expected_manifest_source_ids
            and registry_normalized_ids == expected_manifest_source_ids
            and registry_enriched_ids == expected_manifest_source_ids
            and fts_card_ids == expected_manifest_source_ids
        )
        fts_registry_exact, exact_index_errors = _exact_index_reconciliation(
            enriched_cards,
            normalized_meta,
            current_snapshot,
            youtube_segment_paths,
        )
        errors.extend(f"exact_index:{item}" for item in exact_index_errors)
    except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as exc:
        errors.append(
            safe_exception_token(
                "current_state_reconciliation_failed",
                exc,
                component="release_verifier",
            )
        )
    media = _media_contract()
    provenance_counts = provenance.get("counts", {}) if isinstance(provenance.get("counts"), dict) else {}
    tests = _test_evidence(
        tests_payload,
        evidence_paths,
        trusted_git_snapshot=trusted_git,
        release_identity=bundle_identity,
    )
    identity = manifest.get("pipeline_identity") if isinstance(manifest.get("pipeline_identity"), dict) else {}
    expected_identity = {
        "llm_profile": config.LLM_PROFILE,
        "enrichment_model": llm_backend.active_model_for("enrichment"),
        "media_contract_version": MEDIA_CONTRACT_VERSION,
        "late_fusion_enabled": config.LATE_FUSION_ENABLED,
        "reranker_enabled": config.RERANKER_ENABLED,
        "wiki_enabled": config.WIKI_ENABLED,
        "expected_cards": len(manifest_sources),
        "config_identity_sha256": config_identity_sha256(),
    }
    manifest_run_id = str(manifest.get("run_id") or "")
    load_manifest_path = str(load_report.get("manifest_path") or "")
    expected_loaded_sources = sorted(
        [
            {
                "source_id": str(source.get("source_id") or ""),
                "graph_text_sha256": str(source.get("graph_text_sha256") or "").lower(),
            }
            for source in manifest_sources
        ],
        key=lambda item: item["source_id"],
    )
    try:
        tree_status = git_status_porcelain()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        tree_status = None
        errors.append(
            safe_exception_token(
                "release_tree_status_error",
                exc,
                component="release_verifier",
            )
        )
    checks = {
        "clean_release_tree": (tree_status == ""),
        "manifest_load": (
            load_report.get("status") == "accepted"
            and load_report.get("expected") == len(manifest_sources)
            and load_report.get("inserted") == len(manifest_sources)
            and load_report.get("skipped") == 0
            and load_report.get("failed") == 0
            and str(load_report.get("run_id") or "") == str(manifest_run_id)
            and bool(load_manifest_path)
            and load_report.get("manifest_sha256") == manifest_sha
            and load_report.get("loaded_sources") == expected_loaded_sources
        ),
        "manifest_identity": (
            manifest.get("schema_version") == "accepted_manifest_v2"
            and all(identity.get(field) == value for field, value in expected_identity.items())
        ),
        "manifest_files_fresh": manifest_files_fresh,
        "media_contract": media["card_sidecar_mismatches"] == 0,
        "graph_processed": status_counts.get("processed", 0) == len(manifest_sources)
        and status_counts.get("processing", 0) == 0
            and status_counts.get("failed", 0) == 0,
        "graph_exact_sets": graph_exact_sets,
        "provenance_reconciliation": (
            provenance.get("records") == len(manifest_sources)
            and provenance_counts.get("matched") == provenance.get("records")
            and provenance_counts.get("missing", 0) == 0
            and provenance_counts.get("ambiguous", 0) == 0
            and provenance_counts.get("nonterminal", 0) == 0
            and provenance_exact_sets
            and provenance_fresh
            and state_snapshot_match
        ),
        "fts_registry": (
            fts.get("cards_fts") == len(enriched_cards)
            and fts.get("youtube_segments_fts") == len(youtube_segment_paths)
            and _wiki_fts_gate(fts)
            and registry.get("normalized_docs") == len(normalized_txt)
            and registry.get("enriched_cards") == len(enriched_cards)
            and registry.get("sources", 0) >= registry.get("enriched_cards", 0)
            and registry.get("references", 0) > 0
            and registry.get("references_with_urls") == registry.get("references")
        ),
        "fts_registry_exact": fts_registry_exact,
        "state_inventory": (
            state_inventory.get("files", 0) > 0
            and isinstance(state_inventory.get("sha256"), str)
            and state_inventory.get("manifest_sha256") == manifest_sha
            and state_inventory.get("corpus_identity_sha256") == bundle_identity.get("corpus_identity_sha256")
        ),
        "state_snapshot": state_snapshot_match,
        "current_provenance_fresh": provenance_fresh,
        "backup": backup.get("status") == "passed"
        and backup_snapshot_fresh
        and _positive_int(backup.get("files_declared"))
        and backup.get("files_declared") == backup.get("files_checked")
        and not backup.get("errors"),
        "portable_paths": (
            portable.get("status") in {"passed", "ok"}
            and portable_snapshot_fresh
            and _positive_int(portable.get("scanned_files") or portable.get("scanned"))
            and not portable.get("unresolved", [])
            and not portable.get("changed_files", [])
            and not portable.get("field_changes", [])
        ),
        "security": (
            security.get("status") == "passed"
            and security.get("finding_files", 1) == 0
            and security.get("occurrences", 1) == 0
            and security.get("protected_occurrences", 1) == 0
            and security.get("metadata_occurrences", 1) == 0
            and _protected_files_are_clean(security.get("protected_files"))
            and security.get("unreadable_files", 1) == 0
            and _zero_or_empty(security.get("skipped_files", 1))
            and _zero_or_empty(security.get("tracked_protected_files", 1))
            and _zero_or_empty(security.get("staged_protected_files", 1))
            and _zero_or_empty(security.get("git_inspection_errors", 1))
            and security_inventory_fresh
        ),
        "late_fusion_smoke": bool(
            smoke.get("passed") is True
            and _smoke_is_strict(smoke)
            and _smoke_ledger_is_strict(smoke, evidence_paths.get("smoke_attempt_ledger"))
        ),
        "cold_graph": (
            activation.get("cold_graph_attempt", {}).get("status") == "passed"
            and activation.get("cold_graph_attempt", {}).get("expected") == len(manifest_sources)
            and activation.get("cold_graph_attempt", {}).get("processed") == len(manifest_sources)
            and activation.get("cold_graph_attempt", {}).get("inserted_by_loader", 0)
            + activation.get("cold_graph_attempt", {}).get("reused_processed", 0)
            == len(manifest_sources)
            and activation.get("source_storage_copied") is False
            and activation.get("empty_before") is True
            and _cold_graph_is_strict(activation, manifest_sources)
        ),
        "tests": (
            tests.get("status") == "passed"
            and isinstance(tests.get("summary"), dict)
            and (
                (
                    _positive_int(tests.get("summary", {}).get("gates_passed"))
                    and tests.get("summary", {}).get("gates_failed") == 0
                    and tests.get("summary", {}).get("timeouts") == 0
                )
                if "gates_passed" in tests.get("summary", {})
                else (
                    _positive_int(tests.get("summary", {}).get("passed"))
                    and tests.get("summary", {}).get("failed", 0) == 0
                )
            )
            and all(
                isinstance(item, dict) and item.get("status") == "passed" and item.get("exit_code") == 0
                for item in tests.get("checks", [])
            )
        ),
        "token_rotation": risk.get("status") == "closed" and risk.get("completion_basis") == "user_attestation",
        "semantic_markers": marker_count == 0,
    }
    return {
        "schema_version": "release_verification_v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "passed" if not errors and all(checks.values()) else "failed",
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "errors": errors,
        "input_artifact_dir": str(artifact_dir),
        "release_identity": bundle_identity,
        "repository": {
            "root": str(PROJECT_ROOT),
            "branch": _git_value("branch", "--show-current"),
            "commit": _git_value("rev-parse", "HEAD"),
        },
        "runtime": {
            "llm_profile": config.LLM_PROFILE,
            "active_model": f"codex-cli:{config.CODEX_LUNA_MODEL}@xhigh" if config.LLM_PROFILE == "luna" else "current",
            "late_fusion_enabled": config.LATE_FUSION_ENABLED,
            "reranker_enabled": config.RERANKER_ENABLED,
            "wiki_enabled": config.WIKI_ENABLED,
            "codex_fallback_to_api": config.CODEX_FALLBACK_TO_API,
            "ytdlp_require_current": config.YTDLP_REQUIRE_CURRENT,
            "media_contract_version": MEDIA_CONTRACT_VERSION,
        },
        "token_rotation": {
            "risk_id": "RISK-TOKEN-ROTATION-001",
            "status": risk.get("status"),
            "completion_basis": risk.get("completion_basis"),
            "user_attestation_recorded": checks["token_rotation"],
            "secret_values_in_report": artifact_scan.get("status") != "passed",
        },
        "corpus": {
            "normalized_txt": len(normalized_txt),
            "normalized_meta": len(normalized_meta),
            "enriched_cards": len(enriched_cards),
            "quality_flags": flags,
            "semantic_technical_markers": marker_count,
            "media": media,
        },
        "fts": fts,
        "registry": registry,
        "state_inventory": state_inventory,
        "manifest": {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path) if manifest_path.is_file() else None,
            "sources": len(manifest_sources),
            "load_report": load_report,
        },
        "graph": {
            "live_storage": str(config.RAG_STORAGE_DIR),
            "full_docs": len(full_docs),
            "processed": status_counts.get("processed", 0),
            "processing": status_counts.get("processing", 0),
            "failed": status_counts.get("failed", 0),
            "entities": len(full_entities),
            "relations": len(full_relations),
            "text_chunks": len(text_chunks),
            "provenance_reconciliation": provenance,
            "current_provenance_reconciliation": current_reconciliation,
            "current_state_snapshot": current_snapshot,
            "current_source_proof": current_source_proof,
            "exact_doc_ids": sorted(expected_doc_ids),
            "exact_source_ids": sorted(expected_source_ids),
            "activation_report": activation,
            "cold_graph_attempt": activation.get("cold_graph_attempt", {}),
        },
        "backup": {
            "validation": {
                "status": backup.get("status"),
                "files_declared": backup.get("files_declared"),
                "files_checked": backup.get("files_checked"),
                "nested_paths": len(backup.get("nested_paths", [])),
                "errors": backup.get("errors", []),
            },
            "external_quarantine": backup.get("external_quarantine"),
        },
        "portable_paths": portable,
        "security": {
            "release_scan": security,
            "inventory_fresh": security_inventory_fresh,
            "artifact_scan": artifact_scan,
            "artifact_scan_zero_findings": artifact_scan.get("status") == "passed",
            "raw_credentials_published": False,
        },
        "late_fusion_smoke": smoke,
        "tests": tests,
    }


def _token_rotation_markdown_line(report: dict[str, Any]) -> str:
    token_rotation = report.get("token_rotation")
    token_rotation_closed = (
        isinstance(token_rotation, dict)
        and token_rotation.get("user_attestation_recorded") is True
        and isinstance(report.get("checks"), dict)
        and report["checks"].get("token_rotation") is True
    )
    token_rotation_label = (
        "closed by explicit user attestation"
        if token_rotation_closed
        else "NOT CLOSED: explicit user attestation missing or invalid"
    )
    return f"- Token rotation/revocation: **{token_rotation_label}**; no secret value is stored here."


def _prepare_verification_publication(
    report: dict[str, Any], release_run_id: str, output_dir: Path
) -> tuple[dict[str, Any], Path]:
    if report.get("release_identity", {}).get("release_run_id") != release_run_id:
        report["errors"].append("release_run_id_mismatch")
        report["status"] = "failed"
        report["failed_checks"] = sorted(
            set(report["failed_checks"] + ["release_identity"])
        )
    sanitized, redaction_count = sanitize_release_value(report)
    if not isinstance(sanitized, dict):
        raise RuntimeError("release report sanitization returned an invalid root")
    if redaction_count:
        sanitized["status"] = "failed"
        sanitized.setdefault("errors", []).append(
            f"release_secret_material_redacted:count={redaction_count}"
        )
        checks = sanitized.setdefault("checks", {})
        checks["release_artifact_secrets"] = False
        sanitized["failed_checks"] = sorted(
            set(sanitized.setdefault("failed_checks", []) + ["release_artifact_secrets"])
        )
    if not re.fullmatch(r"attempt-\d{3}", output_dir.name):
        raise RuntimeError(
            "release verification --run-dir must use canonical attempt-NNN name"
        )
    if output_dir.exists():
        raise RuntimeError("release verification run already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent)
    )
    return sanitized, temporary_dir


def _best_effort_verification_failure(
    output_dir: Path,
    release_run_id: str,
    primary_exc: BaseException,
    secondary_exc: BaseException,
) -> Path | None:
    """Publish sanitized primary/secondary diagnostics without replacing either."""
    failed_dir = output_dir.parent / (
        f"{output_dir.name}.failed-{os.getpid()}-{os.urandom(4).hex()}"
    )
    failed_tmp: Path | None = None
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_tmp = Path(
            tempfile.mkdtemp(prefix=f".{failed_dir.name}.tmp-", dir=output_dir.parent)
        )
        primary_diagnostic = safe_exception_diagnostic(
            "verification_interrupted",
            primary_exc,
            component="final_release_verifier",
        )
        secondary_diagnostic = safe_exception_diagnostic(
            "verification_finalization_failed",
            secondary_exc,
            component="final_release_publication",
        )
        failure_report = {
            "schema_version": "release_verification_failure_v1",
            "release_run_id": release_run_id,
            "status": "failed",
            **secondary_diagnostic,
            "primary_failure": primary_diagnostic,
            "secondary_failure": secondary_diagnostic,
        }
        failed_json = failed_tmp / "failed_release_verification.json"
        failed_md = failed_tmp / "failed_release_verification.md"
        atomic_write_bytes(failed_json, canonical_json(failure_report) + b"\n")
        atomic_write_bytes(
            failed_md,
            (
                "# Release verification failed\n\n"
                f"Primary error code: {primary_diagnostic['error_code']}\n"
                f"Primary exception type: {primary_diagnostic['exception_type']}\n"
                f"Secondary error code: {secondary_diagnostic['error_code']}\n"
                f"Secondary exception type: {secondary_diagnostic['exception_type']}\n"
            ).encode(),
        )
        failure_manifest = {
            "schema_version": "release_verification_failure_manifest_v1",
            "release_run_id": release_run_id,
            "json": {"path": failed_json.name, "bytes": failed_json.stat().st_size, "sha256": sha256_file(failed_json)},
            "markdown": {"path": failed_md.name, "bytes": failed_md.stat().st_size, "sha256": sha256_file(failed_md)},
        }
        atomic_write_bytes(
            failed_tmp / "verification_manifest.json",
            canonical_json(failure_manifest) + b"\n",
        )
        with prepared_publication_tree(failed_tmp, failed_dir) as publication:
            prepared_dir = publication.io_path
            durable_prepare_tree(prepared_dir)
            analyzed_snapshot = capture_release_artifact_tree_snapshot(prepared_dir)
            failure_scan = scan_release_artifact_tree(
                prepared_dir, analyzed_snapshot=analyzed_snapshot
            )
            if failure_scan.get("status") != "passed":
                raise RuntimeError("failure artifact secret scan failed")
            _validate_verification_publication_snapshot(
                analyzed_snapshot,
                {
                    "failed_release_verification.json",
                    "failed_release_verification.md",
                    "verification_manifest.json",
                },
            )
            publication.seal(str(failure_scan["inventory_sha256"]))
            publication.commit()
        return failed_dir
    except ReleasePublicationCommittedError:
        # The interrupt still wins, but never delete or report the already
        # committed failure artifact as if publication had not happened.
        return failed_dir
    except BaseException:
        if failed_tmp is not None:
            shutil.rmtree(failed_tmp, ignore_errors=True)
        return None


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--base-ref", default=TRUSTED_RELEASE_BASE_REF, choices=(TRUSTED_RELEASE_BASE_REF,))
    args = parser.parse_args()
    input_dir = safe_resolve_release_path(
        args.input_dir, label="release_verification_input_path"
    )
    output_dir = safe_resolve_release_path(
        args.run_dir, label="release_verification_run_path"
    )
    pending_interrupt: KeyboardInterrupt | asyncio.CancelledError | None = None
    try:
        report = build_report(input_dir, args.release_run_id, trusted_base_ref=args.base_ref)
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        pending_interrupt = exc
        diagnostic = safe_exception_diagnostic(
            "verifier_runtime_error",
            exc,
            component="final_release_verifier",
        )
        report = {
            "schema_version": "release_verification_v2",
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "failed",
            "checks": {"verifier_runtime": False, "late_fusion_smoke": False},
            "failed_checks": ["verifier_runtime"],
            "errors": [
                ":".join(
                    (diagnostic["error_code"], diagnostic["exception_type"], diagnostic["message_sha256"])
                )
            ],
            "input_artifact_dir": str(input_dir),
            "release_identity": {},
            "corpus": {
                "normalized_txt": 0,
                "normalized_meta": 0,
                "enriched_cards": 0,
                "quality_flags": {},
                "semantic_technical_markers": -1,
                "media": {"total": 0, "images": 0, "videos": 0, "card_sidecar_mismatches": -1},
            },
            "graph": {"processed": 0, "current_source_proof": {}, "provenance_reconciliation": {}},
            "manifest": {"sources": 0, "load_report": {}},
            "security": {"release_scan": {"scanned_files": 0, "finding_files": -1}},
            "fts": {},
            "registry": {},
            "tests": {"status": "failed"},
            "backup": {},
            "portable_paths": {},
            "late_fusion_smoke": {},
        }
    except Exception as exc:  # pragma: no cover - release guard
        diagnostic = safe_exception_diagnostic(
            "verifier_runtime_error",
            exc,
            component="final_release_verifier",
        )
        report = {
            "schema_version": "release_verification_v2",
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "failed",
            "checks": {"verifier_runtime": False, "late_fusion_smoke": False},
            "failed_checks": ["verifier_runtime"],
            "errors": [
                ":".join(
                    (diagnostic["error_code"], diagnostic["exception_type"], diagnostic["message_sha256"])
                )
            ],
            "input_artifact_dir": str(input_dir),
            "release_identity": {},
            "corpus": {
                "normalized_txt": 0,
                "normalized_meta": 0,
                "enriched_cards": 0,
                "quality_flags": {},
                "semantic_technical_markers": -1,
                "media": {"total": 0, "images": 0, "videos": 0, "card_sidecar_mismatches": -1},
            },
            "graph": {"processed": 0, "current_source_proof": {}, "provenance_reconciliation": {}},
            "manifest": {"sources": 0, "load_report": {}},
            "security": {"release_scan": {"scanned_files": 0, "finding_files": -1}},
            "fts": {},
            "registry": {},
            "tests": {"status": "failed"},
            "backup": {},
            "portable_paths": {},
            "late_fusion_smoke": {},
        }
    preparation_error: BaseException | None = None
    temporary_dir: Path | None = None
    try:
        report, temporary_dir = _prepare_verification_publication(
            report, args.release_run_id, output_dir
        )
    except BaseException as exc:
        preparation_error = exc
    if preparation_error is not None:
        if pending_interrupt is not None:
            best_effort_with_pending_interrupt(
                pending_interrupt,
                lambda: _best_effort_verification_failure(
                    output_dir,
                    args.release_run_id,
                    pending_interrupt,
                    preparation_error,
                ),
            )
            raise pending_interrupt
        raise preparation_error
    assert temporary_dir is not None
    final_output_dir = output_dir
    committed_uncertain: dict[str, Any] | None = None
    failure_artifact_publication: dict[str, Any] | None = None
    failed_dir: Path | None = None

    def _manifest_for(directory: Path) -> dict[str, Any]:
        json_path = directory / "final_release_verification.json"
        markdown_path = directory / "final_release_verification.md"
        return {
            "schema_version": "release_verification_manifest_v1",
            "release_run_id": args.release_run_id,
            "json": {"path": json_path.name, "bytes": json_path.stat().st_size, "sha256": sha256_file(json_path)},
            "markdown": {"path": markdown_path.name, "bytes": markdown_path.stat().st_size, "sha256": sha256_file(markdown_path)},
        }

    try:
        output = temporary_dir / "final_release_verification.json"
        markdown = temporary_dir / "final_release_verification.md"
        report_bytes = canonical_json(report) + b"\n"
        assert_no_release_secrets(report_bytes, label="final_release_verification.json")
        atomic_write_bytes(output, report_bytes)
        status_label = "PASSED" if report["status"] == "passed" else "FAILED"
        markdown_bytes = (
            "\n".join(
                [
                    "# Final release verification", "", f"Status: **{status_label}**", "",
                    f"- Corpus: {report['corpus']['normalized_txt']} normalized text / {report['corpus']['enriched_cards']} Enriched cards.",
                    f"- Media: {report['corpus']['media']['total']} items; sidecar/card mismatches: {report['corpus']['media']['card_sidecar_mismatches']}.",
                    f"- Graph: {report['graph']['processed']}/{report['manifest']['sources']} documents processed.",
                    f"- Manifest load: expected={report['manifest']['load_report'].get('expected')}, inserted={report['manifest']['load_report'].get('inserted')}, failed={report['manifest']['load_report'].get('failed')}.",
                    f"- Security: scanned {report['security']['release_scan'].get('scanned_files')} files, findings={report['security']['release_scan'].get('finding_files')}.",
                    f"- Late-Fusion smoke: {'PASS' if report['checks']['late_fusion_smoke'] else 'FAILED/NOT RUN'}.",
                    f"- Failed checks: {', '.join(report['failed_checks']) or 'none'}.",
                    _token_rotation_markdown_line(report), "",
                    "This verifier is fail-closed: a report with any failed or missing gate is not a release approval.",
                ]
            ) + "\n"
        ).encode("utf-8")
        assert_no_release_secrets(markdown_bytes, label="final_release_verification.md")
        atomic_write_bytes(markdown, markdown_bytes)
        verification_manifest = _manifest_for(temporary_dir)
        atomic_write_json(temporary_dir / "verification_manifest.json", verification_manifest)
        # Self-check all three publication artifacts before the directory can
        # become visible under its canonical final name.
        with prepared_publication_tree(temporary_dir, final_output_dir) as publication:
            prepared_dir = publication.io_path
            durable_prepare_tree(prepared_dir)
            analyzed_snapshot = capture_release_artifact_tree_snapshot(prepared_dir)
            final_scan = scan_release_artifact_tree(
                prepared_dir, analyzed_snapshot=analyzed_snapshot
            )
            if final_scan.get("status") != "passed":
                raise RuntimeError("final verification artifact secret scan failed")
            _validate_verification_publication_snapshot(
                analyzed_snapshot,
                {
                    "final_release_verification.json",
                    "final_release_verification.md",
                    "verification_manifest.json",
                },
            )
            publication.seal(str(final_scan["inventory_sha256"]))
            publication.commit()
        output = final_output_dir / "final_release_verification.json"
        markdown = final_output_dir / "final_release_verification.md"
        exit_code = 0 if report["status"] == "passed" else 1
    except ReleasePublicationCommittedError as exc:
        committed_uncertain = {
            **publication_outcome(exc),
            "output_dir": str(final_output_dir),
        }
        receipt = output_dir.parent / (
            f"{output_dir.name}.committed-uncertain-{os.getpid()}-"
            f"{os.urandom(4).hex()}.json"
        )
        try:
            diagnostic = safe_exception_diagnostic(
                "verification_publication_committed_uncertain",
                exc,
                component="final_release_publication",
            )
            atomic_write_release_json(
                receipt,
                {
                    "schema_version": "release_verification_failure_v1",
                    "release_run_id": args.release_run_id,
                    **committed_uncertain,
                    **diagnostic,
                },
                label="verification_publication_committed_uncertain",
            )
            committed_uncertain["receipt"] = str(receipt)
        except BaseException:
            pass
        exit_code = 1
    except BaseException as exc:
        import shutil

        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) and pending_interrupt is None:
            pending_interrupt = exc

        shutil.rmtree(temporary_dir, ignore_errors=True)
        failed_dir = output_dir.parent / f"{output_dir.name}.failed-{os.getpid()}-{os.urandom(4).hex()}"
        failed_tmp = Path(tempfile.mkdtemp(prefix=f".{failed_dir.name}.tmp-", dir=output_dir.parent))
        try:
            diagnostic = safe_exception_diagnostic(
                "verification_publication_failed",
                exc,
                component="final_release_publication",
            )
            failure_report = {
                "schema_version": "release_verification_failure_v1",
                "release_run_id": args.release_run_id,
                "status": "failed",
                **diagnostic,
                "final_output_dir": str(output_dir),
            }
            atomic_write_bytes(failed_tmp / "failed_release_verification.json", canonical_json(failure_report) + b"\n")
            atomic_write_bytes(
                failed_tmp / "failed_release_verification.md",
                (
                    "# Release verification failed\n\n"
                    f"Error code: {diagnostic['error_code']}\n"
                    f"Exception type: {diagnostic['exception_type']}\n"
                ).encode(),
            )
            failed_json = failed_tmp / "failed_release_verification.json"
            failed_md = failed_tmp / "failed_release_verification.md"
            failure_manifest = {
                "schema_version": "release_verification_failure_manifest_v1",
                "release_run_id": args.release_run_id,
                "json": {"path": failed_json.name, "bytes": failed_json.stat().st_size, "sha256": sha256_file(failed_json)},
                "markdown": {"path": failed_md.name, "bytes": failed_md.stat().st_size, "sha256": sha256_file(failed_md)},
            }
            atomic_write_bytes(failed_tmp / "verification_manifest.json", canonical_json(failure_manifest) + b"\n")
            try:
                with prepared_publication_tree(failed_tmp, failed_dir) as publication:
                    prepared_dir = publication.io_path
                    durable_prepare_tree(prepared_dir)
                    analyzed_snapshot = capture_release_artifact_tree_snapshot(
                        prepared_dir
                    )
                    failure_scan = scan_release_artifact_tree(
                        prepared_dir, analyzed_snapshot=analyzed_snapshot
                    )
                    if failure_scan.get("status") != "passed":
                        raise RuntimeError("failure artifact secret scan failed")
                    _validate_verification_publication_snapshot(
                        analyzed_snapshot,
                        {
                            "failed_release_verification.json",
                            "failed_release_verification.md",
                            "verification_manifest.json",
                        },
                    )
                    publication.seal(str(failure_scan["inventory_sha256"]))
                    publication.commit()
            except ReleasePublicationCommittedError as publication_exc:
                failure_artifact_publication = {
                    **publication_outcome(publication_exc),
                    "path": str(failed_dir),
                }
                failed_tmp = None
            else:
                failure_artifact_publication = {
                    "status": "published",
                    "committed": True,
                    "path": str(failed_dir),
                }
                failed_tmp = None
        except BaseException as failure_publication_exc:
            import shutil
            failure_artifact_publication = {
                **publication_outcome(failure_publication_exc),
                "path": str(failed_dir),
            }
            if failed_tmp is not None and not failure_artifact_publication["committed"]:
                shutil.rmtree(failed_tmp, ignore_errors=True)
        exit_code = 1
    if pending_interrupt is not None:
        raise pending_interrupt
    if committed_uncertain is not None:
        print(json.dumps(committed_uncertain, ensure_ascii=False))
    elif exit_code == 0:
        print(json.dumps({"status": report["status"], "report": str(final_output_dir / "final_release_verification.json"), "markdown": str(final_output_dir / "final_release_verification.md")}, ensure_ascii=False))
    elif final_output_dir.exists():
        print(json.dumps({"status": report["status"], "report": str(final_output_dir / "final_release_verification.json"), "markdown": str(final_output_dir / "final_release_verification.md")}, ensure_ascii=False))
    else:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_artifact": str(failed_dir) if failed_dir is not None else None,
                    "failure_artifact_publication": failure_artifact_publication,
                },
                ensure_ascii=False,
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="build_release_verification"))
