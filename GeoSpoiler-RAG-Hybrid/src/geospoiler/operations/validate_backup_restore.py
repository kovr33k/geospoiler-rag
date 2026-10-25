"""Fail-closed backup manifest validator and restore dry-run."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    atomic_write_release_json,
    canonical_json,
    preflight_release_identity,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
    strict_json_loads,
)

_OUTPUT_NAMESPACES = {
    "enriched",
    "enriched_segments",
    "normalized",
    "normalized_youtube",
    "review_queue",
}
BACKUP_SOURCE_MANIFEST_SCHEMA = "backup_source_manifest_v1"
LEGACY_BACKUP_SOURCE_MANIFEST_SCHEMA = "backup_restore_closeout_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_restore_path(relative: Path) -> tuple[Path, list[str]]:
    """Map only known legacy nesting to a project-root-relative restore path."""
    if relative.is_absolute() or relative.drive or not relative.parts:
        raise ValueError("path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("path contains traversal or empty components")

    parts = list(relative.parts)
    rules: list[str] = []
    folded = [part.casefold() for part in parts]
    if len(parts) >= 3 and folded[0] == "rag_storage" and folded[1] == "rag_storage":
        del parts[1]
        rules.append("collapse_repeated_rag_storage_root")

    folded = [part.casefold() for part in parts]
    if (
        len(parts) >= 5
        and folded[0] == "output"
        and folded[1] in _OUTPUT_NAMESPACES
        and folded[2] == folded[3]
    ):
        del parts[3]
        rules.append("collapse_repeated_channel_namespace")

    folded = [part.casefold() for part in parts]
    if folded.count("output") > 1 or folded.count("rag_storage") > 1:
        raise ValueError("ambiguous repeated root namespace")
    return Path(*parts), rules


def _symlink_component(root: Path, relative: Path) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def validate(
    manifest_path: Path, _report_path: Path | None = None
) -> dict[str, Any]:
    """Build a restore-validation report without publishing it."""
    manifest_path = manifest_path.resolve()
    manifest_bytes = manifest_path.read_bytes()
    try:
        payload = strict_json_loads(manifest_bytes.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise RuntimeError("backup manifest JSON is invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise RuntimeError("backup manifest must be an object with a files list")
    if payload.get("schema_version") not in {
        BACKUP_SOURCE_MANIFEST_SCHEMA,
        LEGACY_BACKUP_SOURCE_MANIFEST_SCHEMA,
    }:
        raise RuntimeError("unsupported backup source manifest schema")
    root_value = payload.get("root")
    root = Path(root_value).resolve() if isinstance(root_value, str) and root_value else manifest_path.parent
    errors: list[str] = []
    seen_physical: set[str] = set()
    seen_restore: dict[str, str] = {}
    checked = 0
    nested_paths: list[str] = []
    canonical_mapping: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    for index, record in enumerate(payload["files"]):
        if not isinstance(record, dict):
            errors.append(f"files[{index}] is not an object")
            continue
        relative = record.get("path")
        if not isinstance(relative, str) or not relative.strip():
            errors.append(f"files[{index}] path is missing")
            continue
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive or any(
            part in {"", ".", ".."} for part in candidate.parts
        ):
            errors.append(f"files[{index}] path escapes backup root: {relative}")
            continue
        symlink = _symlink_component(root, candidate)
        if symlink is not None:
            errors.append(f"files[{index}] symlink component is forbidden: {relative}")
            continue
        try:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(
                f"files[{index}] path escapes backup root: {relative}:"
                + safe_exception_token(
                    "backup_path_escape",
                    exc,
                    component="validate_backup_restore",
                )
            )
            continue
        physical_key = str(resolved).casefold()
        if physical_key in seen_physical:
            errors.append(f"files[{index}] duplicate path: {relative}")
        seen_physical.add(physical_key)
        try:
            restore_path, rules = _canonical_restore_path(candidate)
        except ValueError as exc:
            errors.append(
                f"files[{index}] ambiguous restore path: {relative}:"
                + safe_exception_token(
                    "backup_restore_path_ambiguous",
                    exc,
                    component="validate_backup_restore",
                )
            )
            continue
        restore_key = restore_path.as_posix().casefold()
        previous = seen_restore.get(restore_key)
        if previous is not None:
            errors.append(
                f"files[{index}] duplicate canonical restore path: {relative} conflicts with {previous}"
            )
        else:
            seen_restore[restore_key] = relative
        if rules:
            nested_paths.append(relative)
        canonical_mapping.append(
            {
                "backup_path": relative,
                "restore_path": restore_path.as_posix(),
                "rules": rules,
            }
        )
        if not resolved.is_file():
            errors.append(f"files[{index}] missing backup file: {relative}")
            continue
        observed_size = resolved.stat().st_size
        observed_sha = _sha256(resolved)
        expected_size = record.get("bytes")
        if not isinstance(expected_size, int) or observed_size != expected_size:
            errors.append(f"files[{index}] byte-size mismatch: {relative}")
        expected_sha = str(record.get("sha256") or "").lower()
        if len(expected_sha) != 64 or observed_sha != expected_sha:
            errors.append(f"files[{index}] SHA-256 mismatch: {relative}")
        inventory.append(
            {
                "path": candidate.as_posix(),
                "bytes": observed_size,
                "sha256": observed_sha,
            }
        )
        checked += 1
    inventory.sort(key=lambda item: item["path"].casefold())
    report = {
        "schema_version": "backup_restore_closeout_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "backup_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "backup_inventory_sha256": hashlib.sha256(canonical_json(inventory)).hexdigest(),
        "root": str(root),
        "files_declared": len(payload["files"]),
        "files_checked": checked,
        "nested_paths": nested_paths,
        "canonical_mapping": canonical_mapping,
        "errors": errors,
        "status": "passed" if not errors else "failed",
        "mode": "dry_run",
    }
    if errors:
        raise RuntimeError("backup restore validation failed: " + "; ".join(errors))
    return report


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--release-run-id")
    args = parser.parse_args()
    manifest_path = safe_resolve_release_path(
        args.manifest, label="backup_manifest_path"
    )
    report_path = safe_resolve_release_path(args.report, label="backup_report_path")
    preflight = None
    if args.release_manifest is not None:
        if not args.release_run_id:
            parser.error("--release-run-id is required with --release-manifest")
        preflight = preflight_release_identity(
            args.release_run_id, args.release_manifest
        )
    report = validate(manifest_path)
    report["schema_version"] = "backup_restore_closeout_v1"
    if preflight is not None:
        identity = preflight.identity
        report = stamp_report(
            report,
            identity,
            f"python scripts/validate_backup_restore.py --manifest {args.manifest} --report {args.report} --release-manifest {args.release_manifest} --release-run-id {args.release_run_id}",
            producer="scripts.validate_backup_restore",
            producer_schema_version="backup_restore_closeout_v1",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=[
                "python", "scripts/validate_backup_restore.py", "--manifest", str(args.manifest),
                "--report", str(args.report), "--release-manifest", str(args.release_manifest),
                "--release-run-id", args.release_run_id,
            ],
        )
    atomic_write_release_json(report_path, report, label="backup_restore_report")
    print(json.dumps({key: report[key] for key in ("status", "files_declared", "files_checked")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="validate_backup_restore"))
