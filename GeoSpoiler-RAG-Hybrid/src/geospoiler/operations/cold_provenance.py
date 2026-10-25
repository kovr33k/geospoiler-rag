"""Independent file and LightRAG state proofs for an isolated cold run."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    canonical_json,
    safe_exception_token,
    strict_json_loads,  # noqa: E402
)
from geospoiler.retrieval.lightrag.ingest import _prepare_text_for_rag  # noqa: E402
from geospoiler.retrieval.lightrag.manifest_contract import validate_manifest_payload  # noqa: E402
from geospoiler.retrieval.lightrag.storage import (
    _canonical_source_path,
    _source_doc_id,  # noqa: E402
)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def sha256_file(path: Path) -> str:
    """Hash a file without loading the complete file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resolve_manifest_file(value: object, field: str, *, strict_roots: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve(strict=False)
    current = path
    has_symlink = False
    while True:
        if current.is_symlink():
            has_symlink = True
            break
        if current == current.parent:
            break
        current = current.parent
    if has_symlink or resolved.is_symlink():
        raise ValueError(f"{field} must not be a symlink: {value}")
    if strict_roots:
        roots = (
            (config.NORMALIZED_DIR, config.YOUTUBE_NORMALIZED_DIR)
            if "normalized_path" in field
            else (config.ENRICHED_DIR,)
        )
        if not any(_is_relative_to(resolved, root.resolve(strict=False)) for root in roots):
            raise ValueError(f"{field} outside allowed corpus roots: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"{field} is missing: {resolved}")
    return resolved


def _proof_hash(payload: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != field}
    return _sha256_bytes(canonical_json(unsigned))


def compute_source_proof(manifest: dict[str, Any]) -> dict[str, Any]:
    """Measure manifest files and graph text from the files themselves.

    The manifest hashes are treated only as expected values.  Every measured
    digest is recalculated from the current normalized/card files, and the
    card provenance plus exact source/doc sets are checked at the same time.
    """
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError("cold source proof requires a non-empty manifest.sources")
    strict_roots = manifest.get("schema_version") == "accepted_manifest_v2"
    shared = None
    if strict_roots:
        shared = validate_manifest_payload(manifest)
        if shared.errors:
            raise ValueError("accepted manifest validation failed")
        sources = shared.entries

    records: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    normalized_paths: set[str] = set()
    card_paths: set[str] = set()
    doc_ids: set[str] = set()
    for index, entry in enumerate(sources):
        if not isinstance(entry, dict):
            raise ValueError(f"sources[{index}] must be an object")
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError(f"sources[{index}] source_id is missing")
        if shared is not None:
            normalized_path = shared.normalized_paths[source_id]
            card_path = shared.card_paths[source_id]
        else:
            normalized_path = _resolve_manifest_file(entry.get("normalized_path"), f"sources[{index}].normalized_path", strict_roots=False)
            card_path = _resolve_manifest_file(entry.get("card_path"), f"sources[{index}].card_path", strict_roots=False)
        canonical_normalized = _canonical_source_path(str(normalized_path))
        doc_id = _source_doc_id(canonical_normalized)
        normalized_key = str(normalized_path)
        card_key = str(card_path)
        if source_id in source_ids:
            raise ValueError(f"duplicate source_id: {source_id}")
        if normalized_key in normalized_paths:
            raise ValueError(f"duplicate normalized path: {normalized_path}")
        if card_key in card_paths:
            raise ValueError(f"duplicate card path: {card_path}")
        if doc_id in doc_ids:
            raise ValueError(f"duplicate doc_id: {doc_id}")

        card_read_error: ValueError | None = None
        try:
            card = strict_json_loads(card_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            diagnostic = safe_exception_token(
                "cold_card_read_failed",
                exc,
                component="cold_provenance",
            )
            card_read_error = ValueError(f"cannot read card {card_path}:{diagnostic}")
        if card_read_error is not None:
            raise card_read_error
        if not isinstance(card, dict):
            raise ValueError(f"card root must be an object: {card_path}")
        if card.get("schema_version") != "enriched_v2":
            raise ValueError(f"card is not enriched_v2: {card_path}")
        provenance = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
        if provenance.get("source_id") != source_id:
            raise ValueError(f"sources[{index}] card source_id mismatch")
        if provenance.get("normalized_path") != entry.get("normalized_path"):
            raise ValueError(f"sources[{index}] card normalized_path mismatch")
        graph_text = card.get("graph_text")
        if not isinstance(graph_text, str) or not graph_text.strip():
            raise ValueError(f"sources[{index}] card graph_text is missing")

        measured_normalized = sha256_file(normalized_path)
        measured_enriched = sha256_file(card_path)
        measured_graph = _sha256_bytes(graph_text.encode("utf-8"))
        expected_hashes = {
            "normalized_sha256": entry.get("normalized_sha256"),
            "enriched_sha256": entry.get("enriched_sha256"),
            "graph_text_sha256": entry.get("graph_text_sha256"),
        }
        measured_hashes = {
            "normalized_sha256": measured_normalized,
            "enriched_sha256": measured_enriched,
            "graph_text_sha256": measured_graph,
        }
        for field, expected in expected_hashes.items():
            if not isinstance(expected, str) or expected.lower() != measured_hashes[field]:
                raise ValueError(
                    f"sources[{index}] {field} mismatch: expected {expected!r}, measured {measured_hashes[field]!r}"
                )

        graph_text_for_load = _prepare_text_for_rag(graph_text.strip())
        if not graph_text_for_load:
            raise ValueError(f"sources[{index}] graph_text becomes empty for RAG load")
        source_ids.add(source_id)
        normalized_paths.add(normalized_key)
        card_paths.add(card_key)
        doc_ids.add(doc_id)
        records.append(
            {
                "source_id": source_id,
                "normalized_path": entry.get("normalized_path"),
                "card_path": entry.get("card_path"),
                "doc_id": doc_id,
                "normalized_sha256": measured_normalized,
                "enriched_sha256": measured_enriched,
                "graph_text_sha256": measured_graph,
                "graph_text_for_load_sha256": _sha256_bytes(graph_text_for_load.encode("utf-8")),
                "normalized_bytes": normalized_path.stat().st_size,
                "enriched_bytes": card_path.stat().st_size,
                "graph_text_bytes": len(graph_text.encode("utf-8")),
            }
        )
    records.sort(key=lambda item: str(item["source_id"]))
    proof: dict[str, Any] = {
        "schema_version": "cold_source_proof_v1",
        "source_ids": sorted(source_ids),
        "doc_ids": sorted(doc_ids),
        "records": records,
    }
    proof["source_proof_sha256"] = _proof_hash(proof, "source_proof_sha256")
    return proof


def _full_doc_content(record: object) -> str | None:
    if isinstance(record, str):
        return record
    if not isinstance(record, dict):
        return None
    value = record.get("content")
    if isinstance(value, str):
        return value
    value = record.get("text")
    return value if isinstance(value, str) else None


def _read_object(path: Path, label: str) -> dict[str, Any]:
    read_error: ValueError | None = None
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        diagnostic = safe_exception_token(
            "cold_storage_read_failed",
            exc,
            component="cold_provenance",
        )
        read_error = ValueError(f"cannot read {label}:{diagnostic}")
    if read_error is not None:
        raise read_error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def loaded_doc_proof(source_proof: dict[str, Any], target: Path) -> dict[str, Any]:
    """Check exact LightRAG status/full-doc sets and hash loaded content."""
    expected_doc_ids = set(source_proof.get("doc_ids") or [])
    expected_source_ids = set(source_proof.get("source_ids") or [])
    if not expected_doc_ids or not expected_source_ids:
        raise ValueError("source proof has no expected source/doc IDs")
    status = _read_object(target / "kv_store_doc_status.json", "cold doc status")
    full_docs = _read_object(target / "kv_store_full_docs.json", "cold full docs")
    if set(status) != expected_doc_ids:
        raise ValueError("cold status doc_id set mismatch")
    if set(full_docs) != expected_doc_ids:
        raise ValueError("cold full_docs doc_id set mismatch")

    expected_by_id = {str(record["doc_id"]): record for record in source_proof["records"]}
    records: list[dict[str, Any]] = []
    for doc_id in sorted(expected_doc_ids):
        status_record = status.get(doc_id)
        if not isinstance(status_record, dict):
            raise ValueError(f"cold status record missing: {doc_id}")
        status_value = str(status_record.get("status") or "").casefold().rsplit(".", 1)[-1]
        if status_value != "processed":
            raise ValueError(f"cold doc is not processed: {doc_id}={status_value!r}")
        content = _full_doc_content(full_docs.get(doc_id))
        if content is None:
            raise ValueError(f"cold full-doc content missing: {doc_id}")
        actual_hash = _sha256_bytes(content.encode("utf-8"))
        expected = expected_by_id[doc_id]
        expected_hash = expected["graph_text_for_load_sha256"]
        if actual_hash != expected_hash:
            raise ValueError(f"cold full-doc content hash mismatch: {doc_id}")
        records.append(
            {
                "source_id": expected["source_id"],
                "doc_id": doc_id,
                "status": "processed",
                "loaded_full_doc_sha256": actual_hash,
                "loaded_full_doc_bytes": len(content.encode("utf-8")),
                "graph_text_for_load_sha256": expected_hash,
            }
        )
    records.sort(key=lambda item: str(item["source_id"]))
    proof: dict[str, Any] = {
        "schema_version": "cold_loaded_doc_proof_v1",
        "source_proof_sha256": source_proof.get("source_proof_sha256"),
        "source_ids": sorted(expected_source_ids),
        "doc_ids": sorted(expected_doc_ids),
        "records": records,
    }
    proof["loaded_doc_proof_sha256"] = _proof_hash(proof, "loaded_doc_proof_sha256")
    return proof


def storage_inventory(target: Path) -> dict[str, Any]:
    """Build a deterministic, symlink-safe inventory of cold storage files."""
    raw_target = target
    target_is_symlink = raw_target.is_symlink()
    target = raw_target.resolve(strict=False)
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    if target_is_symlink:
        errors.append("cold storage target must not be a symlink")
    if target.exists() and not target.is_dir():
        errors.append("cold storage target must be a directory")

    def inside(path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(target)
            return True
        except ValueError:
            return False

    if target.is_dir() and not errors:
        for root, directories, files in os.walk(target, followlinks=False):
            root_path = Path(root)
            if not inside(root_path):
                errors.append(f"storage path escapes target: {root_path}")
                continue
            for name in sorted(directories):
                candidate = root_path / name
                if candidate.is_symlink() or not inside(candidate):
                    errors.append(f"storage symlink/path escape: {candidate}")
            for name in sorted(files):
                candidate = root_path / name
                if candidate.is_symlink() or not inside(candidate):
                    errors.append(f"storage symlink/path escape: {candidate}")
                    continue
                try:
                    relative = candidate.relative_to(target).as_posix()
                    size = candidate.stat().st_size
                    records.append({"path": relative, "bytes": size, "sha256": sha256_file(candidate)})
                except OSError as exc:
                    diagnostic = safe_exception_token(
                        "cold_storage_file_unreadable",
                        exc,
                        component="cold_provenance",
                    )
                    errors.append(f"storage file unreadable:{candidate}:{diagnostic}")
    records.sort(key=lambda item: str(item["path"]).casefold())
    inventory: dict[str, Any] = {
        "schema_version": "cold_storage_inventory_v1",
        "target": str(target),
        "checked": not errors,
        "files": len(records),
        "bytes": sum(int(item["bytes"]) for item in records),
        "records": records,
        "errors": sorted(errors),
    }
    inventory["inventory_sha256"] = _proof_hash(inventory, "inventory_sha256")
    return inventory
