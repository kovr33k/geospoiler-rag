"""Initialize an isolated copy of LightRAG and verify terminal doc status counts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from geospoiler import config  # noqa: E402
from geospoiler.cli.runtime import _finalize_rag_safely  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.cold_provenance import compute_source_proof, loaded_doc_proof, storage_inventory
from geospoiler.operations.release_identity import (
    COLD_GRAPH_BATCH_SIZE_DEFAULT,
    COLD_GRAPH_BATCH_SIZE_MAX,
    COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT,
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_release_json,
    build_identity,
    canonical_json,
    config_identity_sha256,
    effective_cold_runtime,
    preflight_release_identity,
    run_release_cli,
    safe_exception_diagnostic,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
    strict_json_loads,
)
from geospoiler.retrieval.lightrag import ingest as ingest_module  # noqa: E402
from geospoiler.retrieval.lightrag import storage as storage_module  # noqa: E402
from geospoiler.retrieval.lightrag.factory import create_rag  # noqa: E402
from geospoiler.retrieval.lightrag.ingest import load_from_manifest  # noqa: E402
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file  # noqa: E402
from geospoiler.retrieval.lightrag.storage import (
    _canonical_source_path,
    _source_doc_id,  # noqa: E402
)

CHECKPOINT_SCHEMA = "cold_graph_checkpoint_v2"
DEFAULT_BATCH_SIZE = COLD_GRAPH_BATCH_SIZE_DEFAULT
MAX_BATCH_SIZE = COLD_GRAPH_BATCH_SIZE_MAX
DEFAULT_BATCH_TIMEOUT_SECONDS = COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT
_STORAGE_MARKERS = frozenset(
    {
        "kv_store_doc_status.json",
        "kv_store_full_docs.json",
        "kv_store_full_entities.json",
        "kv_store_full_relations.json",
        "kv_store_text_chunks.json",
        "doc_metadata_index.sqlite",
        "doc_metadata_index.json",
    }
)


def _status_value(record: object) -> str:
    if not isinstance(record, dict):
        return "unknown"
    return str(record.get("status") or "unknown").casefold().rsplit(".", 1)[-1]


def _status_counts(status_payload: object) -> dict[str, int]:
    if not isinstance(status_payload, dict):
        return {}
    counts: dict[str, int] = {}
    for record in status_payload.values():
        status = _status_value(record)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _timestamp_seconds(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def _source_duration_seconds(record: object) -> float | None:
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    started = _timestamp_seconds(metadata.get("processing_start_time"))
    finished = _timestamp_seconds(metadata.get("processing_end_time"))
    if started is None:
        started = _timestamp_seconds(record.get("created_at"))
    if finished is None:
        finished = _timestamp_seconds(record.get("updated_at"))
    if started is None or finished is None or finished < started:
        return None
    return round(finished - started, 3)


def _source_path_for_doc_id(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    raw_path = entry.get("normalized_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return ""
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return _canonical_source_path(str(path))


def _source_results(
    manifest: object,
    status_payload: object,
    *,
    include_extended: bool = False,
) -> list[dict[str, object]]:
    """Join immutable manifest sources with LightRAG's per-document status rows."""
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list):
        return []
    statuses = status_payload if isinstance(status_payload, dict) else {}
    results: list[dict[str, object]] = []
    for index, entry in enumerate(sources):
        source = entry if isinstance(entry, dict) else {}
        source_path = _source_path_for_doc_id(source)
        doc_id = _source_doc_id(source_path) if source_path else None
        record = statuses.get(doc_id) if doc_id else None
        status = _status_value(record)
        terminal = status in {"processed", "failed"}
        error: str | None = None
        if isinstance(record, dict):
            raw_error = record.get("error_msg") or record.get("error") or record.get("message")
            if raw_error:
                error = str(raw_error)
        if status == "failed" and not error:
            error = "LightRAG marked document as failed"
        elif status == "unknown":
            error = "No LightRAG status record found for manifest source"
        duration_seconds = _source_duration_seconds(record)
        result: dict[str, object] = {
            "index": index,
            "source_id": source.get("source_id"),
            "normalized_path": source.get("normalized_path"),
            "doc_id": doc_id,
            "status": status,
            "terminal_status": status if terminal else None,
            "duration_seconds": duration_seconds,
            "error": error,
        }
        if include_extended:
            result.update(
                {
                    "normalized_sha256": source.get("normalized_sha256"),
                    "enriched_sha256": source.get("enriched_sha256"),
                    "graph_text_sha256": source.get("graph_text_sha256"),
                    "duration_ms": (round(duration_seconds * 1000) if duration_seconds is not None else None),
                }
            )
        results.append(result)
    return results


def _manifest_status_counts(source_results: list[dict[str, object]], status_payload: object) -> dict[str, int]:
    """Count only manifest sources when their doc IDs can be joined safely."""
    joined = [item for item in source_results if item.get("doc_id")]
    if len(joined) == len(source_results) and source_results:
        return dict(Counter(str(item.get("status") or "unknown") for item in source_results))
    # Small mocked/unit manifests may omit paths; retain their status snapshot
    # semantics without weakening the real manifest join.
    return _status_counts(status_payload)


def _attempt_counts(
    expected: int,
    source_results: list[dict[str, object]],
    status_payload: object,
    *,
    inserted_source_ids: set[str] | None = None,
    loader_returned_total: int | None = None,
) -> dict[str, object]:
    counts = _manifest_status_counts(source_results, status_payload)
    processed = counts.get("processed", 0)
    failed = counts.get("failed", 0)
    processing = counts.get("processing", 0)
    processed_source_ids = _source_ids_from_results(source_results, "processed")
    inserted_ids = set(inserted_source_ids or ()) & processed_source_ids
    reused_ids = processed_source_ids - inserted_ids
    return {
        "expected": expected,
        "processed": processed,
        "processing": processing,
        "failed": failed,
        "terminal": processed + failed,
        "status_counts": counts,
        "loader_returned": loader_returned_total,
        "inserted_by_loader": len(inserted_ids),
        "reused_processed": len(reused_ids),
        "processed_terminal": len(processed_source_ids),
        "lineage": {
            "inserted_source_ids": sorted(inserted_ids),
            "reused_processed_source_ids": sorted(reused_ids),
            "processed_terminal_source_ids": sorted(processed_source_ids),
        },
    }


def _validate_batch_size(batch_size: int) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("batch_size must be an integer")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    return batch_size


def _validate_batch_timeout(batch_timeout_seconds: float) -> float:
    """Require a finite, positive wall-clock guard for each isolated batch."""
    if isinstance(batch_timeout_seconds, bool) or not isinstance(batch_timeout_seconds, (int, float)):
        raise ValueError("batch_timeout_seconds must be a number")
    timeout = float(batch_timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("batch_timeout_seconds must be finite and greater than zero")
    return timeout


def _checkpoint_identity(
    manifest_path: Path,
    manifest: dict[str, object],
    batch_size: int,
    batch_timeout_seconds: float,
    source_proof_sha256: str,
    manifest_bytes: bytes | None = None,
) -> dict[str, object]:
    effective_runtime = effective_cold_runtime(batch_size, batch_timeout_seconds)
    snapshot = manifest_bytes if manifest_bytes is not None else manifest_path.read_bytes()
    return {
        "manifest_sha256": hashlib.sha256(snapshot).hexdigest(),
        "run_id": manifest.get("run_id"),
        "config_identity_sha256": config_identity_sha256(),
        "batch_size": batch_size,
        "batch_timeout_seconds": batch_timeout_seconds,
        "effective_cold_runtime": effective_runtime,
        "source_proof_sha256": source_proof_sha256,
        "expected": len(manifest.get("sources") or []),
    }


def _new_checkpoint(
    manifest_path: Path,
    manifest: dict[str, object],
    batch_size: int,
    batch_timeout_seconds: float,
    source_proof: dict[str, object],
    initial_storage_inventory: dict[str, object],
    manifest_bytes: bytes | None = None,
) -> dict[str, object]:
    source_proof_sha256 = str(source_proof.get("source_proof_sha256") or "")
    identity = _checkpoint_identity(
        manifest_path,
        manifest,
        batch_size,
        batch_timeout_seconds,
        source_proof_sha256,
        manifest_bytes,
    )
    return {
        "schema_version": CHECKPOINT_SCHEMA,
        **identity,
        "empty_before": True,
        "completed_source_ids": [],
        "inserted_source_ids": [],
        "source_proof": source_proof,
        "storage_inventory": initial_storage_inventory,
        "loader_returned_total": 0,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _validate_checkpoint(
    checkpoint: object,
    manifest_path: Path,
    manifest: dict[str, object],
    batch_size: int,
    batch_timeout_seconds: float,
    source_proof: dict[str, object],
    manifest_bytes: bytes | None = None,
) -> dict[str, object]:
    if not isinstance(checkpoint, dict):
        raise ValueError("cold graph checkpoint must be an object")
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("cold graph checkpoint schema mismatch")
    expected_identity = _checkpoint_identity(
        manifest_path,
        manifest,
        batch_size,
        batch_timeout_seconds,
        str(source_proof.get("source_proof_sha256") or ""),
        manifest_bytes,
    )
    for field, expected_value in expected_identity.items():
        if checkpoint.get(field) != expected_value:
            raise ValueError(f"cold graph checkpoint {field} mismatch: {checkpoint.get(field)!r} != {expected_value!r}")
    completed = checkpoint.get("completed_source_ids")
    source_ids = {entry.get("source_id") for entry in manifest.get("sources", []) if isinstance(entry, dict)}
    if not isinstance(completed, list) or any(
        not isinstance(source_id, str) or source_id not in source_ids for source_id in completed
    ):
        raise ValueError("cold graph checkpoint completed_source_ids mismatch")
    inserted_source_ids = checkpoint.get("inserted_source_ids")
    if not isinstance(inserted_source_ids, list) or any(
        not isinstance(source_id, str) or source_id not in source_ids for source_id in inserted_source_ids
    ):
        raise ValueError("cold graph checkpoint inserted_source_ids mismatch")
    if not set(inserted_source_ids).issubset(set(completed)):
        raise ValueError("cold graph checkpoint inserted_source_ids must be completed")
    if checkpoint.get("empty_before") is not True:
        raise ValueError("cold graph checkpoint empty_before must be true")
    if checkpoint.get("source_proof") != source_proof:
        raise ValueError("cold graph checkpoint source_proof mismatch")
    if not isinstance(checkpoint.get("storage_inventory"), dict):
        raise ValueError("cold graph checkpoint storage_inventory missing")
    loader_returned_total = checkpoint.get("loader_returned_total")
    if (
        not isinstance(loader_returned_total, int)
        or isinstance(loader_returned_total, bool)
        or loader_returned_total < 0
    ):
        raise ValueError("cold graph checkpoint loader_returned_total invalid")
    return checkpoint


def _read_checkpoint(path: Path) -> dict[str, object]:
    read_error: ValueError | None = None
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        diagnostic = safe_exception_token(
            "cold_checkpoint_read_failed",
            exc,
            component="cold_graph",
        )
        read_error = ValueError(diagnostic)
    if read_error is not None:
        raise read_error
    if not isinstance(payload, dict):
        raise ValueError("cold graph checkpoint root must be an object")
    return payload


def _write_checkpoint(path: Path, checkpoint: dict[str, object]) -> None:
    payload = dict(checkpoint)
    payload["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write_report(path, payload)


def _batch_manifest(manifest: dict[str, object], entries: list[dict[str, object]]) -> dict[str, object]:
    payload = copy.deepcopy(manifest)
    payload["sources"] = entries
    pipeline_identity = payload.get("pipeline_identity")
    if isinstance(pipeline_identity, dict):
        pipeline_identity["expected_cards"] = len(entries)
    return payload


def _write_batch_manifest(
    workspace: Path,
    manifest: dict[str, object],
    entries: list[dict[str, object]],
    batch_index: int,
) -> Path:
    batch_dir = workspace / "cold_graph_batches"
    path = batch_dir / f"batch-{batch_index:05d}.manifest.json"
    _atomic_write_report(path, _batch_manifest(manifest, entries))
    return path


def _source_ids_from_results(source_results: list[dict[str, object]], status: str) -> set[str]:
    return {str(item["source_id"]) for item in source_results if item.get("source_id") and item.get("status") == status}


def _attach_provenance_proof(
    source_results: list[dict[str, object]],
    source_proof: dict[str, object],
    loaded_proof: dict[str, object],
) -> list[dict[str, object]]:
    measured_by_source = {
        str(record["source_id"]): record for record in source_proof.get("records", []) if isinstance(record, dict)
    }
    loaded_by_source = {
        str(record["source_id"]): record for record in loaded_proof.get("records", []) if isinstance(record, dict)
    }
    enriched: list[dict[str, object]] = []
    for result in source_results:
        source_id = str(result.get("source_id") or "")
        measured = measured_by_source.get(source_id, {})
        loaded = loaded_by_source.get(source_id, {})
        item = dict(result)
        item.update(
            {
                "measured_normalized_sha256": measured.get("normalized_sha256"),
                "measured_enriched_sha256": measured.get("enriched_sha256"),
                "measured_graph_text_sha256": measured.get("graph_text_sha256"),
                "graph_text_for_load_sha256": measured.get("graph_text_for_load_sha256"),
                "loaded_full_doc_sha256": loaded.get("loaded_full_doc_sha256"),
                "loaded_full_doc_matches": (
                    loaded.get("loaded_full_doc_sha256") == measured.get("graph_text_for_load_sha256")
                    and bool(loaded)
                ),
            }
        )
        enriched.append(item)
    return enriched


def _workspace_storage_inventory(workspace: Path, target: Path) -> dict[str, object]:
    """Detect storage-like files copied beside the isolated cold target."""
    root = workspace.resolve()
    target = target.resolve()
    storage_like: list[str] = []
    scanned = 0
    try:
        if root.exists():
            for path in root.rglob("*"):
                resolved = path.resolve(strict=False)
                if resolved == target or target in resolved.parents:
                    continue
                scanned += 1
                if path.is_dir() and path.name.casefold() in {"rag_storage", "rag_storage_backups"}:
                    storage_like.append(str(path.relative_to(root)))
                elif path.is_file() and (
                    path.name in _STORAGE_MARKERS
                    or path.name.casefold().startswith("kv_store_")
                    or path.name.casefold().startswith("vdb_")
                ):
                    storage_like.append(str(path.relative_to(root)))
    except (OSError, RuntimeError, ValueError) as exc:
        diagnostic = safe_exception_diagnostic(
            "workspace_inventory_error", exc, component="cold_graph"
        )
        return {
            "checked": False,
            "workspace": str(root),
            "target": str(target),
            "scanned_entries": scanned,
            "storage_like_paths": sorted(storage_like),
            "source_storage_copied": None,
            "error": ":".join(
                (
                    diagnostic["error_code"],
                    diagnostic["exception_type"],
                    diagnostic["message_sha256"],
                )
            ),
        }
    return {
        "checked": True,
        "workspace": str(root),
        "target": str(target),
        "scanned_entries": scanned,
        "storage_like_paths": sorted(storage_like),
        "source_storage_copied": bool(storage_like),
    }


def _read_status_snapshot(target: Path) -> tuple[dict[str, object], str | None]:
    status_path = target / "kv_store_doc_status.json"
    try:
        payload = strict_json_loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        diagnostic = safe_exception_diagnostic(
            "cannot_read_lightrag_status", exc, component="cold_graph"
        )
        return {}, ":".join(
            (
                diagnostic["error_code"],
                diagnostic["exception_type"],
                diagnostic["message_sha256"],
            )
        )
    if not isinstance(payload, dict):
        return {}, "LightRAG status snapshot root must be an object"
    return payload, None


def _atomic_write_report(path: Path, payload: dict[str, object]) -> None:
    """Write evidence atomically so interruption cannot leave truncated JSON."""
    atomic_write_release_json(path, payload, label="cold_graph_report")


def _manifest_snapshot(manifest_path: Path) -> tuple[dict[str, object], int, str | None]:
    validation = load_manifest_file(manifest_path)
    manifest = validation.payload
    if not isinstance(manifest, dict):
        return {}, 0, None
    sources = manifest.get("sources")
    expected = len(sources) if isinstance(sources, list) else 0
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest() if manifest_path.is_file() else None
    return manifest, expected, manifest_sha


def _load_cold_manifest(path: Path, *, require_release_contract: bool = False) -> dict[str, object]:
    """Load a cold manifest, sharing the accepted contract on release paths.

    The standalone cold-check helper historically supported a small unversioned
    fixture format.  It is retained for non-release diagnostics only.  Every
    ``accepted_manifest_v2`` input, and every manifest used with a release run
    id, goes through the same validator as the loader and verifier.
    """
    if require_release_contract:
        validation = load_manifest_file(path)
        if validation.errors:
            raise ValueError("accepted manifest validation failed")
        return validation.payload
    read_error: ValueError | None = None
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        diagnostic = safe_exception_token(
            "cold_manifest_read_failed",
            exc,
            component="cold_graph",
        )
        read_error = ValueError(diagnostic)
    if read_error is not None:
        raise read_error
    if not isinstance(payload, dict):
        raise ValueError("cold graph manifest root must be an object")
    if payload.get("schema_version") == "accepted_manifest_v2":
        validation = load_manifest_file(path)
        if validation.errors:
            raise ValueError("accepted manifest validation failed")
        return validation.payload
    return payload


def _load_cold_manifest_snapshot(
    path: Path,
    *,
    require_release_contract: bool = False,
) -> tuple[dict[str, object], bytes]:
    """Load the manifest payload together with the exact bytes used for it."""
    if require_release_contract:
        validation = load_manifest_file(path)
        if validation.errors:
            raise ValueError("accepted manifest validation failed")
        if validation.manifest_bytes is None:
            raise ValueError("accepted manifest byte snapshot is missing")
        return validation.payload, validation.manifest_bytes
    raw = path.read_bytes()
    payload = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cold graph manifest root must be an object")
    if payload.get("schema_version") == "accepted_manifest_v2":
        validation = load_manifest_file(path)
        if validation.errors:
            raise ValueError("accepted manifest validation failed")
        if validation.manifest_bytes is None:
            raise ValueError("accepted manifest byte snapshot is missing")
        return validation.payload, validation.manifest_bytes
    return payload, raw


def _failed_report(
    *,
    workspace: Path,
    manifest_path: Path,
    initial_empty_before: bool,
    exc: BaseException,
    elapsed_seconds: float,
    checkpoint_path: Path | None = None,
    resume: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_timeout_seconds: float = DEFAULT_BATCH_TIMEOUT_SECONDS,
) -> dict[str, object]:
    manifest, expected, manifest_sha = _manifest_snapshot(manifest_path)
    target_path = workspace / "rag_storage"
    target = target_path.resolve()
    workspace_inventory = _workspace_storage_inventory(workspace, target)
    status_payload, snapshot_error = _read_status_snapshot(target)
    diagnostic = safe_exception_diagnostic(
        "cold_graph_runner_failed",
        exc,
        component="cold_graph",
    )
    safe_error = ":".join(
        (diagnostic["error_code"], diagnostic["exception_type"], diagnostic["message_sha256"])
    )
    errors = [safe_error]
    if snapshot_error:
        errors.append(snapshot_error)
    source_results = _source_results(manifest, status_payload, include_extended=True)
    try:
        source_proof = compute_source_proof(manifest)
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as proof_error:
        source_proof = None
        proof_diagnostic = safe_exception_diagnostic(
            "source_proof_unavailable", proof_error, component="cold_graph"
        )
        errors.append(
            ":".join(
                (
                    proof_diagnostic["error_code"],
                    proof_diagnostic["exception_type"],
                    proof_diagnostic["message_sha256"],
                )
            )
        )
    checkpoint_inserted_ids: set[str] = set()
    checkpoint_loader_returned = None
    actual_checkpoint = checkpoint_path or workspace / "cold_graph_checkpoint.json"
    if actual_checkpoint.is_file():
        try:
            checkpoint_payload = _read_checkpoint(actual_checkpoint)
            checkpoint_inserted_ids = {str(value) for value in checkpoint_payload.get("inserted_source_ids", [])}
            checkpoint_loader_returned = checkpoint_payload.get("loader_returned_total")
        except ValueError:
            pass
    attempt = _attempt_counts(
        expected,
        source_results,
        status_payload,
        inserted_source_ids=checkpoint_inserted_ids,
        loader_returned_total=checkpoint_loader_returned if isinstance(checkpoint_loader_returned, int) else None,
    )
    target_inventory = storage_inventory(target_path)
    try:
        current_config_identity = config_identity_sha256()
    except Exception:
        current_config_identity = None
    try:
        effective_runtime = effective_cold_runtime(batch_size, batch_timeout_seconds)
    except Exception:
        effective_runtime = None
    return {
        "schema_version": "cold_graph_extraction_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "failed",
        "reason": "runner_exception",
        "exception_type": diagnostic["exception_type"],
        "exception_message": safe_error,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "workspace": str(workspace),
        "candidate_path": str(target),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "manifest_run_id": manifest.get("run_id") if isinstance(manifest, dict) else None,
        "config_identity_sha256": current_config_identity,
        "effective_cold_runtime": effective_runtime,
        "source_proof_before_load": source_proof or {},
        "source_proof_sha256": source_proof.get("source_proof_sha256") if source_proof else None,
        "loaded_doc_proof": {},
        "storage_inventory": target_inventory,
        "source_ids": source_proof.get("source_ids", []) if source_proof else [],
        "doc_ids": source_proof.get("doc_ids", []) if source_proof else [],
        "source_storage_copied": workspace_inventory.get("source_storage_copied"),
        "workspace_inventory": workspace_inventory,
        "empty_before": initial_empty_before,
        "complete": False,
        "resume": resume,
        "batch_size": batch_size,
        "batch_timeout_seconds": batch_timeout_seconds,
        "checkpoint_path": str((checkpoint_path or workspace / "cold_graph_checkpoint.json").resolve()),
        "accepted_manifest_load_report_path": str((workspace / "accepted_manifest_load_report.json").resolve()),
        "errors": errors,
        "source_results": source_results,
        "cold_graph_attempt": {"status": "failed", **attempt, "sources": source_results},
    }


async def _check(
    workspace: Path,
    manifest_path: Path,
    *,
    resume: bool = False,
    checkpoint_path: Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_timeout_seconds: float = DEFAULT_BATCH_TIMEOUT_SECONDS,
    require_release_contract: bool = False,
) -> dict:
    batch_size = _validate_batch_size(batch_size)
    batch_timeout_seconds = _validate_batch_timeout(batch_timeout_seconds)
    started_at = time.perf_counter()
    workspace = workspace.resolve()
    manifest_path = manifest_path.resolve()
    target_path = workspace / "rag_storage"
    target = target_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest, manifest_bytes = _load_cold_manifest_snapshot(
        manifest_path,
        require_release_contract=require_release_contract,
    )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError("cold graph requires a non-empty accepted manifest")
    expected = len(sources)
    source_proof = compute_source_proof(manifest)
    initial_inventory = _workspace_storage_inventory(workspace, target)
    if initial_inventory.get("source_storage_copied") is not False:
        raise RuntimeError("cold workspace inventory is not clean: source storage detected or inventory failed")
    initial_storage_inventory = storage_inventory(target_path)
    if not initial_storage_inventory.get("checked"):
        raise RuntimeError("cold storage inventory is not clean")
    checkpoint_path = (checkpoint_path or workspace / "cold_graph_checkpoint.json").resolve()
    loader_returned_total = 0
    if resume:
        if not target.exists():
            raise FileNotFoundError(f"cannot resume without cold graph storage: {target}")
        checkpoint = _validate_checkpoint(
            _read_checkpoint(checkpoint_path),
            manifest_path,
            manifest,
            batch_size,
            batch_timeout_seconds,
            source_proof,
            manifest_bytes,
        )
        if checkpoint.get("storage_inventory") != initial_storage_inventory:
            raise ValueError("cold graph checkpoint storage_inventory mismatch")
        empty_before = checkpoint["empty_before"] is True
        completed = {str(value) for value in checkpoint["completed_source_ids"]}
        inserted_source_ids = {str(value) for value in checkpoint["inserted_source_ids"]}
        loader_returned_total = int(checkpoint["loader_returned_total"])
    else:
        if target.exists():
            raise FileExistsError(f"Cold graph workspace already exists: {target}")
        if checkpoint_path.exists():
            raise FileExistsError(f"Cold graph checkpoint already exists: {checkpoint_path}")
        empty_before = True
        checkpoint = _new_checkpoint(
            manifest_path,
            manifest,
            batch_size,
            batch_timeout_seconds,
            source_proof,
            initial_storage_inventory,
            manifest_bytes,
        )
        _write_checkpoint(checkpoint_path, checkpoint)
        completed = set()
        inserted_source_ids = set()

    initial_status_payload, initial_status_error = _read_status_snapshot(target)
    if initial_status_error is None:
        initial_results = _source_results(manifest, initial_status_payload, include_extended=True)
        completed.update(_source_ids_from_results(initial_results, "processed"))
    pending = [entry for entry in sources if isinstance(entry, dict) and entry.get("source_id") not in completed]
    accepted_report_path = workspace / "accepted_manifest_load_report.json"
    skipped_report_path = workspace / "rag_insert_skipped.md"
    patched = patch.object(config, "RAG_STORAGE_DIR", target)
    report_patches = (
        patch.object(ingest_module, "_accepted_manifest_report_path", return_value=accepted_report_path),
        patch.object(ingest_module, "_skipped_insert_report_path", return_value=skipped_report_path),
        patch.object(storage_module, "_skipped_insert_report_path", return_value=skipped_report_path),
    )
    with patched, report_patches[0], report_patches[1], report_patches[2]:
        rag = await create_rag()
        try:
            for batch_index, offset in enumerate(range(0, len(pending), batch_size)):
                batch_entries = pending[offset : offset + batch_size]
                batch_manifest_path = _write_batch_manifest(workspace, manifest, batch_entries, batch_index)
                try:
                    if resume:
                        loader_returned = await asyncio.wait_for(
                            load_from_manifest(rag, batch_manifest_path, reuse_processed=True),
                            timeout=batch_timeout_seconds,
                        )
                    else:
                        loader_returned = await asyncio.wait_for(
                            load_from_manifest(rag, batch_manifest_path),
                            timeout=batch_timeout_seconds,
                        )
                except BaseException:
                    interrupted_inventory = storage_inventory(target_path)
                    if interrupted_inventory.get("checked"):
                        checkpoint["storage_inventory"] = interrupted_inventory
                        checkpoint["last_batch"] = {
                            "index": batch_index,
                            "source_ids": [entry.get("source_id") for entry in batch_entries],
                            "status": "interrupted",
                        }
                        _write_checkpoint(checkpoint_path, checkpoint)
                    raise
                if (
                    not isinstance(loader_returned, int)
                    or isinstance(loader_returned, bool)
                    or loader_returned < 0
                ):
                    raise RuntimeError("cold loader returned an invalid count")
                loader_returned_total += loader_returned
                status_payload, snapshot_error = _read_status_snapshot(target)
                if snapshot_error:
                    raise RuntimeError(snapshot_error)
                source_results = _source_results(manifest, status_payload, include_extended=True)
                before_batch_completed = set(completed)
                completed.update(_source_ids_from_results(source_results, "processed"))
                batch_processed = _source_ids_from_results(source_results, "processed")
                batch_source_ids = {str(entry.get("source_id")) for entry in batch_entries if entry.get("source_id")}
                inserted_source_ids.update((batch_processed - before_batch_completed) & batch_source_ids)
                checkpoint["completed_source_ids"] = sorted(completed)
                checkpoint["inserted_source_ids"] = sorted(inserted_source_ids)
                checkpoint["loader_returned_total"] = loader_returned_total
                checkpoint_storage_inventory = storage_inventory(target_path)
                if not checkpoint_storage_inventory.get("checked"):
                    raise RuntimeError("cold storage inventory is not clean")
                checkpoint["storage_inventory"] = checkpoint_storage_inventory
                checkpoint["last_batch"] = {
                    "index": batch_index,
                    "source_ids": [entry.get("source_id") for entry in batch_entries],
                    "loader_returned": loader_returned,
                }
                _write_checkpoint(checkpoint_path, checkpoint)
        finally:
            await _finalize_rag_safely(rag)

    manifest_read_error: RuntimeError | None = None
    try:
        current_manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        diagnostic = safe_exception_token(
            "cold_manifest_unavailable_after_run",
            exc,
            component="cold_graph",
        )
        manifest_read_error = RuntimeError(diagnostic)
    if manifest_read_error is not None:
        raise manifest_read_error
    if current_manifest_bytes != manifest_bytes:
        raise RuntimeError("manifest_changed_during_cold_run")

    status_payload, snapshot_error = _read_status_snapshot(target)
    if snapshot_error:
        raise RuntimeError(snapshot_error)
    source_results = _source_results(manifest, status_payload, include_extended=True)
    loaded_proof = loaded_doc_proof(source_proof, target)
    source_results = _attach_provenance_proof(source_results, source_proof, loaded_proof)
    attempt = _attempt_counts(
        expected,
        source_results,
        status_payload,
        inserted_source_ids=inserted_source_ids,
        loader_returned_total=loader_returned_total,
    )
    workspace_inventory = _workspace_storage_inventory(workspace, target)
    target_inventory = storage_inventory(target_path)
    processed = int(attempt["processed"])
    joined_source_results = [item for item in source_results if item.get("doc_id")]
    source_statuses_joined = len(joined_source_results) == len(source_results) and bool(source_results)
    passed = (
        empty_before
        and processed == expected
        and attempt["processing"] == 0
        and attempt["failed"] == 0
        and workspace_inventory.get("checked") is True
        and workspace_inventory.get("source_storage_copied") is False
        and target_inventory.get("checked") is True
        and set(source_proof.get("source_ids", [])) == {
            str(entry.get("source_id")) for entry in sources if isinstance(entry, dict)
        }
        and set(source_proof.get("doc_ids", [])) == {str(item.get("doc_id")) for item in source_results}
        and all(item.get("loaded_full_doc_matches") is True for item in source_results)
        and (not source_statuses_joined or all(item.get("terminal_status") == "processed" for item in source_results))
    )
    checkpoint["completed_source_ids"] = sorted(_source_ids_from_results(source_results, "processed"))
    checkpoint["inserted_source_ids"] = sorted(inserted_source_ids)
    checkpoint["loader_returned_total"] = loader_returned_total
    checkpoint["storage_inventory"] = target_inventory
    checkpoint["loaded_doc_proof"] = loaded_proof
    _write_checkpoint(checkpoint_path, checkpoint)
    return {
        "schema_version": "cold_graph_extraction_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "status": "passed" if passed else "failed",
        "workspace": str(workspace),
        "candidate_path": str(target),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": manifest_sha256,
        "manifest_run_id": manifest.get("run_id"),
        "config_identity_sha256": config_identity_sha256(),
        "effective_cold_runtime": effective_cold_runtime(batch_size, batch_timeout_seconds),
        "source_proof_before_load": source_proof,
        "source_proof_sha256": source_proof.get("source_proof_sha256"),
        "loaded_doc_proof": loaded_proof,
        "storage_inventory": target_inventory,
        "source_ids": source_proof.get("source_ids", []),
        "doc_ids": source_proof.get("doc_ids", []),
        "source_storage_copied": workspace_inventory.get("source_storage_copied"),
        "workspace_inventory": workspace_inventory,
        "empty_before": empty_before,
        "complete": passed,
        "resume": resume,
        "batch_size": batch_size,
        "batch_timeout_seconds": batch_timeout_seconds,
        "checkpoint_path": str(checkpoint_path),
        "accepted_manifest_load_report_path": str(accepted_report_path.resolve()),
        "source_results": source_results,
        "errors": [],
        "cold_graph_attempt": {
            "status": "passed" if passed else "failed",
            **attempt,
            "source_ids": source_proof.get("source_ids", []),
            "doc_ids": source_proof.get("doc_ids", []),
            "sources": source_results,
        },
    }


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--release-run-id")
    parser.add_argument("--resume", action="store_true", help="Resume only with a matching checkpoint identity.")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="Checkpoint path (defaults to <workspace>/cold_graph_checkpoint.json).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Sources per isolated load batch (1-{MAX_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--batch-timeout-seconds",
        type=float,
        default=DEFAULT_BATCH_TIMEOUT_SECONDS,
        help="Maximum wall-clock seconds allowed for one isolated load batch.",
    )
    args = parser.parse_args()
    workspace = safe_resolve_release_path(args.workspace, label="cold_workspace_path")
    manifest_path = safe_resolve_release_path(
        args.manifest,
        label="release_manifest_path",
    )
    initial_empty_before = not (workspace / "rag_storage").exists()
    checkpoint_path = safe_resolve_release_path(
        args.checkpoint or workspace / "cold_graph_checkpoint.json",
        label="cold_checkpoint_path",
    )
    command = (
        f"python scripts/cold_graph_check.py --workspace {args.workspace} --manifest {args.manifest} "
        f"--report {args.report} --checkpoint {checkpoint_path} --batch-size {args.batch_size}"
        f" --batch-timeout-seconds {args.batch_timeout_seconds:g}"
        + (" --resume" if args.resume else "")
        + (f" --release-run-id {args.release_run_id}" if args.release_run_id else "")
    )
    release_preflight = None
    if args.release_run_id:
        for label, path in (
            ("cold_workspace_path", workspace),
            (
                "cold_report_path",
                safe_resolve_release_path(args.report, label="cold_report_path"),
            ),
            ("cold_checkpoint_path", checkpoint_path),
        ):
            assert_no_release_secrets(str(path), label=label)
        validation = load_manifest_file(manifest_path)
        release_preflight = preflight_release_identity(
            args.release_run_id,
            manifest_path,
            validation=validation,
            identity_builder=build_identity,
        )
    started_at = time.perf_counter()
    pending_interrupt: KeyboardInterrupt | asyncio.CancelledError | None = None
    try:
        report = asyncio.run(
            _check(
                workspace,
                manifest_path,
                resume=args.resume,
                checkpoint_path=checkpoint_path,
                batch_size=args.batch_size,
                batch_timeout_seconds=args.batch_timeout_seconds,
                require_release_contract=bool(args.release_run_id),
            )
        )
        if release_preflight is not None:
            identity = release_preflight.identity
            if identity["manifest_sha256"] != report.get("manifest_sha256"):
                raise RuntimeError("cold report manifest snapshot mismatch")
            report = stamp_report(
                report,
                identity,
                command,
                producer="scripts.cold_graph_check",
                producer_schema_version="cold_graph_extraction_v1",
                source_inputs={
                    "manifest_sha256": identity["manifest_sha256"],
                    "corpus_identity_sha256": identity["corpus_identity_sha256"],
                },
                command_argv=["python", "scripts/cold_graph_check.py", *sys.argv[1:]],
            )
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        pending_interrupt = exc
        report = _failed_report(
            workspace=workspace,
            manifest_path=manifest_path,
            initial_empty_before=initial_empty_before,
            exc=exc,
            elapsed_seconds=time.perf_counter() - started_at,
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            batch_size=args.batch_size,
            batch_timeout_seconds=args.batch_timeout_seconds,
        )
        if release_preflight is not None:
            try:
                identity = release_preflight.identity
                report = stamp_report(
                    report,
                    identity,
                    command,
                    producer="scripts.cold_graph_check",
                    producer_schema_version="cold_graph_extraction_v1",
                    source_inputs={
                        "manifest_sha256": identity["manifest_sha256"],
                        "corpus_identity_sha256": identity["corpus_identity_sha256"],
                    },
                    command_argv=["python", "scripts/cold_graph_check.py", *sys.argv[1:]],
                )
            except Exception as identity_error:
                identity_diagnostic = safe_exception_diagnostic(
                    "release_identity_error",
                    identity_error,
                    component="cold_graph",
                )
                report["release_identity_error"] = ":".join(
                    (
                        identity_diagnostic["error_code"],
                        identity_diagnostic["exception_type"],
                        identity_diagnostic["message_sha256"],
                    )
                )
    except Exception as exc:
        report = _failed_report(
            workspace=workspace,
            manifest_path=manifest_path,
            initial_empty_before=initial_empty_before,
            exc=exc,
            elapsed_seconds=time.perf_counter() - started_at,
            checkpoint_path=checkpoint_path,
            resume=args.resume,
            batch_size=args.batch_size,
            batch_timeout_seconds=args.batch_timeout_seconds,
        )
        if release_preflight is not None:
            try:
                identity = release_preflight.identity
                report = stamp_report(
                    report,
                    identity,
                    command,
                    producer="scripts.cold_graph_check",
                    producer_schema_version="cold_graph_extraction_v1",
                    source_inputs={
                        "manifest_sha256": identity["manifest_sha256"],
                        "corpus_identity_sha256": identity["corpus_identity_sha256"],
                    },
                    command_argv=["python", "scripts/cold_graph_check.py", *sys.argv[1:]],
                )
            except Exception as identity_error:
                identity_diagnostic = safe_exception_diagnostic(
                    "release_identity_error",
                    identity_error,
                    component="cold_graph",
                )
                report["release_identity_error"] = ":".join(
                    (
                        identity_diagnostic["error_code"],
                        identity_diagnostic["exception_type"],
                        identity_diagnostic["message_sha256"],
                    )
                )
    try:
        if release_preflight is not None:
            assert_no_release_secrets(canonical_json(report), label="cold_graph_report")
        _atomic_write_report(
            safe_resolve_release_path(args.report, label="cold_report_path"),
            report,
        )
    except Exception:
        if pending_interrupt is None:
            raise
    if pending_interrupt is not None:
        raise pending_interrupt
    print(json.dumps(report["cold_graph_attempt"], ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="cold_graph_check"))
