"""Pure, read-only accepted-manifest validation shared by release gates.

This module deliberately has no LightRAG calls and never writes a report.  It
is the common path/provenance/hash validator used before a loader, cold proof,
or release verifier is allowed to consume a manifest.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geospoiler import config
from geospoiler.operations.release_identity import redact_release_secrets, safe_exception_token, strict_json_loads
from geospoiler.providers import llm_backend
from geospoiler.retrieval.lightrag.storage import _canonical_source_path, _source_doc_id

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REQUIRED_ROOT = ("schema_version", "run_id", "pipeline_identity", "sources")
_REQUIRED_ENTRY = (
    "accepted", "source_id", "normalized_path", "card_path",
    "normalized_sha256", "enriched_sha256", "graph_text_sha256",
)
_BLOCKED_FLAGS = {"extraction_unstable", "partial_segment_failure"}
_PIPELINE_FIELDS = {
    "llm_profile", "enrichment_model", "media_contract_version",
    "late_fusion_enabled", "reranker_enabled", "wiki_enabled",
    "expected_cards", "config_identity_sha256",
}
_ROOT_FIELDS = {
    "accepted_manifest_v1": frozenset({"schema_version", "run_id", "pipeline_identity", "sources", "generated_at"}),
    "accepted_manifest_v2": frozenset({"schema_version", "run_id", "pipeline_identity", "sources", "generated_at"}),
}
_ALL_ROOT_FIELDS = frozenset().union(*_ROOT_FIELDS.values())
_ENTRY_FIELDS = {
    "accepted_manifest_v1": frozenset(_REQUIRED_ENTRY),
    "accepted_manifest_v2": frozenset((*_REQUIRED_ENTRY, "document_kind", "quality_flags")),
}
_LEGACY_PIPELINE_FIELDS = frozenset({
    "text_model", "enrichment_model", "enrichment_schema", "media_contract_version",
    "media_vision_backend", "prompt_version", "transcription_primary",
    "transcription_fallback", "late_fusion_enabled", "reranker_enabled", "scope",
})
# v2 is the release contract.  Legacy identity fields are accepted only by the
# explicitly opt-in v1 compatibility path.  Keeping them out of v2 matters:
# otherwise two producers can stamp different identities while every consumer
# still considers the manifest structurally valid.
_V2_PIPELINE_ALLOWED = frozenset(_PIPELINE_FIELDS)


def _unknown_fields(value: dict[str, Any], allowed: set[str] | frozenset[str], label: str) -> list[str]:
    return [f"{label} unknown field: {key}" for key in sorted(set(value) - set(allowed))]


@dataclass(frozen=True)
class ValidatedManifestEntry:
    """Immutable file snapshot produced by accepted-manifest validation."""

    source_id: str
    normalized_path: Path
    card_path: Path
    doc_id: str
    normalized_bytes: bytes
    card_bytes: bytes
    graph_text: str
    normalized_sha256: str
    enriched_sha256: str
    graph_text_sha256: str


@dataclass(frozen=True)
class ManifestValidation:
    payload: dict[str, Any]
    entries: list[dict[str, Any]]
    normalized_paths: dict[str, Path]
    card_paths: dict[str, Path]
    doc_ids: dict[str, str]
    errors: list[str]
    manifest_bytes: bytes | None = None
    snapshots: dict[str, ValidatedManifestEntry] | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def snapshot_mismatches(snapshot: ValidatedManifestEntry) -> list[str]:
    """Return file changes detected after accepted-manifest validation."""
    errors: list[str] = []
    try:
        normalized_bytes = snapshot.normalized_path.read_bytes()
    except OSError as exc:
        errors.append(
            safe_exception_token(
                "normalized_file_unreadable_after_validation",
                exc,
                component="manifest_contract",
            )
        )
    else:
        if normalized_bytes != snapshot.normalized_bytes:
            errors.append("normalized bytes changed after validation")

    try:
        card_bytes = snapshot.card_path.read_bytes()
    except OSError as exc:
        errors.append(
            safe_exception_token(
                "card_unreadable_after_validation",
                exc,
                component="manifest_contract",
            )
        )
    else:
        if card_bytes != snapshot.card_bytes:
            errors.append("card bytes changed after validation")
    return errors


def _relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_file(value: Any, roots: tuple[Path, ...], label: str) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty string"
    candidate = config.resolve_project_path(value)
    try:
        current = candidate
        while True:
            if current.is_symlink():
                return None, f"{label} uses a symlink/reparse point"
            if current == current.parent:
                break
            current = current.parent
        resolved = candidate.resolve(strict=False)
        resolved_roots = tuple(root.resolve(strict=False) for root in roots)
    except (OSError, RuntimeError, ValueError):
        return None, f"{label} cannot be resolved safely"
    if not any(_relative(resolved, root) for root in resolved_roots):
        return None, f"{label} outside allowed directory"
    if resolved.is_symlink() or not resolved.is_file():
        return None, f"{label} is not a regular file"
    try:
        with resolved.open("rb"):
            pass
    except OSError:
        return None, f"{label} is not readable"
    return resolved, None


def _pipeline_errors(payload: dict[str, Any], expected_cards: int, allow_legacy: bool) -> list[str]:
    errors: list[str] = []
    schema = payload.get("schema_version")
    if schema not in {"accepted_manifest_v1", "accepted_manifest_v2"}:
        errors.append("manifest schema_version must be one of: accepted_manifest_v1, accepted_manifest_v2")
        return errors
    if schema == "accepted_manifest_v1":
        if not allow_legacy:
            errors.append("legacy_identity_not_release_eligible")
        identity = payload.get("pipeline_identity")
        if not isinstance(identity, dict) or identity.get("profile", identity.get("llm_profile")) != config.LLM_PROFILE:
            errors.append("manifest pipeline_identity profile mismatch")
        if isinstance(identity, dict):
            errors.extend(_unknown_fields(identity, _LEGACY_PIPELINE_FIELDS | {"profile", "llm_profile"}, "manifest pipeline_identity"))
        return errors
    identity = payload.get("pipeline_identity")
    if not isinstance(identity, dict):
        return ["manifest pipeline_identity must be an object"]
    errors.extend(_unknown_fields(identity, _V2_PIPELINE_ALLOWED, "manifest pipeline_identity"))
    missing = sorted(_PIPELINE_FIELDS - set(identity))
    if missing:
        errors.append("manifest pipeline_identity missing required field(s): " + ", ".join(missing))
    expected = {
        "llm_profile": config.LLM_PROFILE,
        "enrichment_model": llm_backend.active_model_for("enrichment"),
        "media_contract_version": "media-contract-v2",
        "late_fusion_enabled": config.LATE_FUSION_ENABLED,
        "reranker_enabled": config.RERANKER_ENABLED,
        "wiki_enabled": config.WIKI_ENABLED,
        "expected_cards": expected_cards,
    }
    from geospoiler.operations.release_identity import config_identity_sha256

    expected["config_identity_sha256"] = config_identity_sha256()
    for field, value in expected.items():
        if identity.get(field) != value:
            errors.append(f"manifest pipeline_identity {field} mismatch")
    return errors


def validate_manifest_payload(
    payload: Any,
    *,
    allow_legacy_manifest: bool = False,
    require_nonempty: bool = True,
) -> ManifestValidation:
    """Validate manifest structure, roots, provenance and byte hashes."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ManifestValidation({}, [], {}, {}, {}, ["manifest root must be an object"])
    schema_hint = payload.get("schema_version")
    # Unknown root fields are rejected even when the schema value itself is
    # unknown.  Otherwise a producer could add a field that one consumer
    # ignores while another consumer interprets it, which is exactly the trust
    # boundary this shared validator is meant to close.
    root_fields = _ROOT_FIELDS.get(schema_hint, _ALL_ROOT_FIELDS)
    errors.extend(_unknown_fields(payload, root_fields, "manifest"))
    missing_root = [field for field in _REQUIRED_ROOT if field not in payload]
    errors.extend(f"manifest missing required field: {field}" for field in missing_root)
    entries = payload.get("sources")
    if not isinstance(entries, list) or (require_nonempty and not entries):
        errors.append("manifest sources must be a non-empty list" if require_nonempty else "manifest sources must be a list")
        entries = []
    run_id = payload.get("run_id")
    if "run_id" in payload and (
        not isinstance(run_id, str) or not run_id.strip()
    ):
        errors.append("manifest run_id must be non-empty string")
    errors.extend(_pipeline_errors(payload, len(entries), allow_legacy_manifest))
    normalized_roots = (config.NORMALIZED_DIR, config.YOUTUBE_NORMALIZED_DIR) if payload.get("schema_version") == "accepted_manifest_v2" else (config.NORMALIZED_DIR,)
    normalized_paths: dict[str, Path] = {}
    card_paths: dict[str, Path] = {}
    doc_ids: dict[str, str] = {}
    snapshots: dict[str, ValidatedManifestEntry] = {}
    seen_source_ids: set[str] = set()
    seen_normalized: set[str] = set()
    seen_cards: set[str] = set()
    seen_docs: set[str] = set()
    valid_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        label = f"sources[{index}]"
        entry_error_start = len(errors)
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        if isinstance(schema_hint, str) and schema_hint in _ENTRY_FIELDS:
            errors.extend(_unknown_fields(entry, _ENTRY_FIELDS[schema_hint], label))
        missing = [field for field in _REQUIRED_ENTRY if field not in entry]
        if missing:
            errors.append(f"{label} missing required field(s): {', '.join(missing)}")
            continue
        if entry.get("accepted") is not True:
            errors.append(f"{label} accepted must be true")
            continue
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            errors.append(f"{label} source_id must be non-empty")
            continue
        normalized, normalized_error = _safe_file(entry.get("normalized_path"), normalized_roots, f"{label}.normalized_path")
        card, card_error = _safe_file(entry.get("card_path"), (config.ENRICHED_DIR,), f"{label}.card_path")
        if normalized_error:
            errors.append(normalized_error)
        if card_error:
            errors.append(card_error)
        if normalized is None or card is None:
            continue
        nkey, ckey = str(normalized).casefold(), str(card).casefold()
        if source_id in seen_source_ids:
            errors.append(f"{label} duplicate source_id")
        if nkey in seen_normalized:
            errors.append(f"{label} duplicate normalized_path")
        if ckey in seen_cards:
            errors.append(f"{label} duplicate card_path")
        doc_id = _source_doc_id(_canonical_source_path(str(normalized)))
        if doc_id in seen_docs:
            errors.append(f"{label} duplicate doc_id")
        seen_source_ids.add(source_id)
        seen_normalized.add(nkey)
        seen_cards.add(ckey)
        seen_docs.add(doc_id)
        for field in ("normalized_sha256", "enriched_sha256", "graph_text_sha256"):
            value = entry.get(field)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                errors.append(f"{label} {field} invalid")
        try:
            normalized_bytes = normalized.read_bytes()
            card_bytes = card.read_bytes()
            card_payload = strict_json_loads(card_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(
                safe_exception_token(
                    "manifest_card_unreadable",
                    exc,
                    component="manifest_contract",
                )
            )
            continue
        if not isinstance(card_payload, dict) or card_payload.get("schema_version") != "enriched_v2":
            errors.append(f"{label} card is not enriched_v2")
            continue
        provenance = card_payload.get("provenance") if isinstance(card_payload.get("provenance"), dict) else {}
        if provenance.get("source_id") != source_id:
            errors.append(f"{label} provenance.source_id mismatch")
        if provenance.get("normalized_path") != entry.get("normalized_path"):
            errors.append(f"{label} provenance.normalized_path mismatch")
        graph_text = card_payload.get("graph_text")
        if not isinstance(graph_text, str) or not graph_text.strip():
            errors.append(f"{label} graph_text missing")
        flags = card_payload.get("quality_flags") or []
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            errors.append(f"{label} quality_flags invalid")
        elif _BLOCKED_FLAGS.intersection(flags):
            errors.append(f"{label} blocked quality flag")
        try:
            normalized_sha256 = _sha256_bytes(normalized_bytes)
            enriched_sha256 = _sha256_bytes(card_bytes)
            graph_text_sha256 = _sha256_bytes(graph_text.encode("utf-8")) if isinstance(graph_text, str) else ""
            if normalized_sha256 != str(entry.get("normalized_sha256", "")).lower():
                errors.append(f"{label} normalized_sha256 mismatch")
            if enriched_sha256 != str(entry.get("enriched_sha256", "")).lower():
                errors.append(f"{label} enriched_sha256 mismatch")
            if isinstance(graph_text, str) and graph_text_sha256 != str(entry.get("graph_text_sha256", "")).lower():
                errors.append(f"{label} graph_text_sha256 mismatch")
        except OSError as exc:
            errors.append(
                safe_exception_token(
                    "manifest_hash_failed",
                    exc,
                    component="manifest_contract",
                )
            )
        valid_entries.append(entry)
        normalized_paths[source_id] = normalized
        card_paths[source_id] = card
        doc_ids[source_id] = doc_id
        if isinstance(graph_text, str) and len(errors) == entry_error_start:
            snapshots[source_id] = ValidatedManifestEntry(
                source_id=source_id,
                normalized_path=normalized,
                card_path=card,
                doc_id=doc_id,
                normalized_bytes=normalized_bytes,
                card_bytes=card_bytes,
                graph_text=graph_text,
                normalized_sha256=normalized_sha256,
                enriched_sha256=enriched_sha256,
                graph_text_sha256=graph_text_sha256,
            )
    return ManifestValidation(payload, valid_entries, normalized_paths, card_paths, doc_ids, errors, snapshots=snapshots)


def load_manifest_file(path: Path, *, allow_legacy_manifest: bool = False) -> ManifestValidation:
    try:
        manifest_bytes = path.read_bytes()
        payload = strict_json_loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return ManifestValidation(
            {},
            [],
            {},
            {},
            {},
            [
                safe_exception_token(
                    "manifest_unreadable",
                    exc,
                    component="manifest_contract",
                )
            ],
        )
    validation = validate_manifest_payload(payload, allow_legacy_manifest=allow_legacy_manifest)
    safe_errors = [redact_release_secrets(error)[0] for error in validation.errors]
    return ManifestValidation(
        validation.payload,
        validation.entries,
        validation.normalized_paths,
        validation.card_paths,
        validation.doc_ids,
        safe_errors,
        manifest_bytes=manifest_bytes,
        snapshots=validation.snapshots,
    )
