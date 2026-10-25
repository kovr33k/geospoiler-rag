"""Scan and migrate persisted project/media paths to checkout-portable values."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.ingestion.normalizer.media_paths import (
    portable_media_path,
    portable_project_path,  # noqa: E402
)
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_bytes,
    atomic_write_release_json,
    build_identity,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
)

MEDIA_KEYS = {"file_path", "source_file_path", "resolved_file_path"}
MEDIA_LIST_KEYS = {"image_paths", "native_media_paths"}
PROJECT_KEYS = {
    "artifact_path",
    "visual_artifact_path",
    "transcript_path",
    "card_path",
    "normalized_file",
    "meta_file",
    "canonical_path",
}
PATH_LIKE_KEYS = PROJECT_KEYS | {"source", "source_path"}
MARKERS = (
    "media_cache/",
    "output/",
    "transcripts/",
    "media_visual/",
    "state/",
    "rag_storage/",
    "artifacts/",
)
LEGACY_ROOTS = (
    str(PROJECT_ROOT),
    r"C:\WikiRag\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid",
)
ABSOLUTE_ROOT_PATTERN = re.compile(
    r"(?i)(?:[a-z]:[\\/])[^\s\]\[\)\}\"']+"
)


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    lowered = normalized.casefold()
    for root in LEGACY_ROOTS:
        root_normalized = root.replace("\\", "/").rstrip("/")
        prefix = root_normalized.casefold() + "/"
        if lowered.startswith(prefix):
            return normalized[len(root_normalized) + 1 :]
    for marker in MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            return normalized[index:]
    return ""


def _portable_project_value(value: Any) -> tuple[Any, bool, str | None]:
    if not isinstance(value, str) or not value.strip():
        return value, False, None
    raw = value.strip()
    if raw.startswith(("http://", "https://")):
        return value, False, None
    relative = _relative_path(raw)
    if relative:
        return relative, relative != value, None
    portable = portable_project_path(raw)
    if portable:
        return portable, portable != value, None
    if Path(raw).is_absolute():
        return value, False, "absolute_path_outside_approved_roots"
    return value, False, None


def _portable_media_value(value: Any) -> tuple[Any, bool, str | None]:
    if not isinstance(value, str) or not value.strip():
        return value, False, None
    portable = portable_media_path(value)
    if portable:
        return portable, portable != value, None
    if Path(value).is_absolute() or "media_cache" in value.casefold():
        return value, False, "media_path_outside_approved_root"
    return value, False, None


def _rewrite_json(value: Any, *, key: str = "") -> tuple[Any, int, list[str]]:
    changes = 0
    unresolved: list[str] = []
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for name, child in value.items():
            if name in MEDIA_KEYS:
                updated, changed, error = _portable_media_value(child)
            elif name in MEDIA_LIST_KEYS and isinstance(child, list):
                updated_items = []
                for item in child:
                    updated_item, changed_item, error_item = _portable_media_value(item)
                    updated_items.append(updated_item)
                    changes += int(changed_item)
                    if error_item:
                        unresolved.append(f"{name}:{error_item}")
                updated, changed, error = updated_items, False, None
            elif name in PATH_LIKE_KEYS:
                updated, changed, error = _portable_project_value(child)
            else:
                updated, nested_changes, nested_errors = _rewrite_json(child, key=name)
                changes += nested_changes
                unresolved.extend(nested_errors)
                changed, error = False, None
            result[name] = updated
            changes += int(changed)
            if error:
                unresolved.append(f"{name}:{error}")
        return result, changes, unresolved
    if isinstance(value, list):
        result = []
        for child in value:
            updated, nested_changes, nested_errors = _rewrite_json(child, key=key)
            result.append(updated)
            changes += nested_changes
            unresolved.extend(nested_errors)
        return result, changes, unresolved
    return value, 0, []


def _rewrite_text(text: str) -> tuple[str, int, list[str]]:
    updated = text
    for root in LEGACY_ROOTS:
        for separator in ("\\", "/"):
            updated = updated.replace(root + separator, "")
    updated = re.sub(r"(?i)(?<![a-z0-9_])(media_cache|output|transcripts|media_visual|state|rag_storage)[\\/]", r"\1/", updated)
    unresolved = []
    for match in ABSOLUTE_ROOT_PATTERN.finditer(updated):
        if _relative_path(match.group(0)):
            unresolved.append(match.group(0))
    return updated, int(updated != text), unresolved


def _candidate_files() -> list[Path]:
    roots = [
        config.NORMALIZED_DIR,
        config.YOUTUBE_NORMALIZED_DIR,
        config.ENRICHED_DIR,
        config.YOUTUBE_SEGMENTS_DIR,
        config.STATE_DIR,
    ]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".json", ".txt"})
    return sorted(set(files), key=lambda path: str(path).casefold())


def _candidate_label(path: Path) -> str:
    resolved = path.resolve()
    for root in (
        config.NORMALIZED_DIR,
        config.YOUTUBE_NORMALIZED_DIR,
        config.ENRICHED_DIR,
        config.YOUTUBE_SEGMENTS_DIR,
        config.STATE_DIR,
    ):
        try:
            relative = resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        return f"{Path(root).name}/{relative.as_posix()}"
    return path.name


def candidate_inventory_sha256(candidates: list[Path] | None = None) -> str:
    """Hash the deterministic relative-path/bytes/content candidate inventory."""
    records = [
        {
            "path": _candidate_label(path),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (candidates if candidates is not None else _candidate_files())
    ]
    records.sort(key=lambda item: (item["path"].casefold(), item["sha256"]))
    return hashlib.sha256(canonical_json(records)).hexdigest()


def scan() -> dict[str, Any]:
    changed_files = 0
    field_changes = 0
    unresolved: list[dict[str, Any]] = []
    candidates = _candidate_files()
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            unresolved.append(
                {
                    "path": str(path),
                    "error": safe_exception_token(
                        "portable_path_read_failed",
                        exc,
                        component="migrate_portable_paths",
                    ),
                }
            )
            continue
        file_changes = 0
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                unresolved.append(
                    {
                        "path": str(path),
                        "error": safe_exception_token(
                            "portable_path_json_invalid",
                            exc,
                            component="migrate_portable_paths",
                        ),
                    }
                )
                continue
            _, changes, errors = _rewrite_json(payload)
            file_changes = changes
            field_changes += changes
            unresolved.extend({"path": str(path), "error": error} for error in errors)
        else:
            _, changes, errors = _rewrite_text(raw)
            file_changes = changes
            field_changes += changes
            unresolved.extend({"path": str(path), "error": error} for error in errors)
        changed_files += int(file_changes > 0)
    return {
        "schema_version": "portable_path_migration_v1",
        "status": "passed" if not unresolved and not field_changes else "changes_pending",
        "project_root": str(PROJECT_ROOT),
        "files_scanned": len(candidates),
        "scanned_files": len(candidates),
        "inventory_sha256": candidate_inventory_sha256(candidates),
        "changed_files": changed_files,
        "field_changes": field_changes,
        "unresolved": unresolved,
    }


def apply(_report_path: Path | None = None) -> dict[str, Any]:
    """Apply portable-path rewrites and return an unpublished report payload."""
    changed_files = 0
    field_changes = 0
    unresolved: list[dict[str, Any]] = []
    candidates = _candidate_files()
    for path in candidates:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                unresolved.append(
                    {
                        "path": str(path),
                        "error": safe_exception_token(
                            "portable_path_json_invalid",
                            exc,
                            component="migrate_portable_paths",
                        ),
                    }
                )
                continue
            updated_payload, changes, errors = _rewrite_json(payload)
            updated = json.dumps(updated_payload, ensure_ascii=False, indent=2) + "\n"
        else:
            updated, changes, errors = _rewrite_text(raw)
        if errors:
            unresolved.extend({"path": str(path), "error": error} for error in errors)
            continue
        if updated != raw:
            atomic_write_bytes(path, updated.encode("utf-8"))
            changed_files += 1
        field_changes += changes
    report = {
        "schema_version": "portable_path_migration_v1",
        "status": "passed" if not unresolved and not field_changes else "changes_pending",
        "mode": "apply",
        "project_root": str(PROJECT_ROOT),
        "files_scanned": len(candidates),
        "scanned_files": len(candidates),
        "inventory_sha256": candidate_inventory_sha256(candidates),
        "changed_files": changed_files,
        "field_changes": field_changes,
        "unresolved": unresolved,
    }
    return report


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("scan", "apply", "verify"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-run-id")
    args = parser.parse_args()
    report_path = safe_resolve_release_path(
        args.report, label="portable_path_report_path"
    )
    preflight = None
    if args.manifest is not None:
        if not args.release_run_id:
            parser.error("--release-run-id is required with --manifest")
        preflight = preflight_release_identity(args.release_run_id, args.manifest)
    report = apply() if args.mode == "apply" else scan()
    if args.mode != "apply":
        report["mode"] = args.mode
    if preflight is not None:
        identity = (
            build_identity(
                args.release_run_id,
                preflight.manifest_path,
                preflight.manifest,
                manifest_bytes=preflight.manifest_bytes,
            )
            if args.mode == "apply"
            else preflight.identity
        )
        report = stamp_report(
            report,
            identity,
            f"python scripts/migrate_portable_paths.py {args.mode} --report {args.report} --manifest {args.manifest} --release-run-id {args.release_run_id}",
            producer="scripts.migrate_portable_paths",
            producer_schema_version="portable_path_migration_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=[
                "python", "scripts/migrate_portable_paths.py", args.mode, "--report", str(args.report),
                "--manifest", str(args.manifest), "--release-run-id", args.release_run_id,
            ],
        )
    atomic_write_release_json(report_path, report, label="portable_path_report")
    print(
        json.dumps(
            {
                **{
                    key: report[key]
                    for key in ("mode", "files_scanned", "changed_files", "field_changes")
                },
                "unresolved_count": len(report["unresolved"]),
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["unresolved"] or (args.mode == "verify" and report["field_changes"]) else 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="migrate_portable_paths"))
