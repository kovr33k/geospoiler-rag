"""Scan publishable project scope for live-looking credential material."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_release_json,
    build_identity,
    canonical_json,
    preflight_release_identity,
    read_pinned_file_bytes,
    redact_release_secrets,
    release_secret_count,
    release_secret_groups,
    release_secret_matches,
    run_release_cli,
    safe_exception_token,
    safe_resolve_release_path,
    stamp_report,
)
from geospoiler.operations.security_artifact_redaction import SQLITE_HEADER, _decode_text, _sqlite_occurrences
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file  # noqa: E402

SYNTHETIC_FIXTURE_VALUES = {
    # Tests deliberately exercise redaction with a harmless example.test value.
    # The allowlist is value-exact; a real token in the same file is still a
    # finding.  No production artifact is allowed here.
    "tests/test_security_artifact_redaction.py": {"access" + "Token=secret"},
    "tests/test_youtube_pipeline.py": {"access" + "Token=secret"},
    "tests/test_security_release_scan.py": {"access" + "Token=secret"},
}

PROTECTED_SECRET_FILES = {".env", ".env.local", ".env.production", ".env.development"}


@dataclass(frozen=True)
class ScanCandidate:
    path: Path | None
    root_kind: str
    relative: str
    error_code: str | None = None
    physical_key: str | None = None


@dataclass(frozen=True)
class ScanSnapshot:
    candidate: ScanCandidate
    data: bytes | None
    error: str | None = None
    identity: tuple[int, int, int, int, int] | None = None


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _safe_path_id(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()
    return f"unknown/{digest[:16]}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _make_candidate(path: Path, root_kind: str, root: Path) -> ScanCandidate:
    try:
        lexical_root = root.absolute()
        lexical_relative = path.absolute().relative_to(lexical_root)
        current = lexical_root
        if _is_link_or_reparse(current):
            return ScanCandidate(None, root_kind, _safe_path_id(path), "candidate_root_escape")
        for part in lexical_relative.parts:
            current = current / part
            if _is_link_or_reparse(current):
                return ScanCandidate(None, root_kind, _safe_path_id(path), "candidate_root_escape")
        resolved_root = root.resolve(strict=False)
        resolved = path.resolve(strict=False)
        relative_path = resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return ScanCandidate(None, root_kind, _safe_path_id(path), "candidate_path_invalid")
    relative = relative_path.as_posix()
    display = relative if root_kind == "project" else f"{root_kind}/{relative}"
    return ScanCandidate(
        resolved,
        root_kind,
        display,
        physical_key=str(resolved).casefold(),
    )


def _coerce_candidate(candidate: ScanCandidate | Path) -> ScanCandidate:
    if isinstance(candidate, ScanCandidate):
        if candidate.path is not None and candidate.physical_key is None:
            try:
                resolved = candidate.path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return ScanCandidate(
                    None,
                    candidate.root_kind,
                    candidate.relative,
                    "candidate_path_invalid",
                )
            return ScanCandidate(
                resolved,
                candidate.root_kind,
                candidate.relative,
                candidate.error_code,
                str(resolved).casefold(),
            )
        return candidate
    roots = [
        ("project", config.PROJECT_ROOT),
        ("output", config.OUTPUT_DIR),
        ("artifacts", config.PROJECT_ROOT / "artifacts"),
        ("source_registry", config.SOURCE_REGISTRY_DB_PATH.parent),
    ]
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ScanCandidate(None, "unknown", _safe_path_id(candidate), "candidate_path_invalid")
    for root_kind, root in roots:
        try:
            resolved_root = root.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _is_relative_to(resolved, resolved_root):
            return _make_candidate(candidate, root_kind, root)
    return ScanCandidate(None, "unknown", _safe_path_id(candidate), "candidate_root_unknown")


def _candidate_files() -> list[ScanCandidate]:
    candidates: list[ScanCandidate] = []
    roots = (
        ("project", config.PROJECT_ROOT, True),
        ("output", config.OUTPUT_DIR, False),
        ("artifacts", config.PROJECT_ROOT / "artifacts", False),
    )
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for root_kind, root, apply_exclusions in roots:
        try:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if apply_exclusions and any(
                    part.casefold() in excluded for part in path.parts
                ):
                    continue
                candidates.append(_make_candidate(path, root_kind, root))
        except (OSError, RuntimeError, ValueError):
            candidates.append(
                ScanCandidate(None, root_kind, f"{root_kind}/[UNAVAILABLE]", "candidate_discovery_failed")
            )
    try:
        if config.SOURCE_REGISTRY_DB_PATH.exists():
            candidates.append(
                _make_candidate(
                    config.SOURCE_REGISTRY_DB_PATH,
                    "source_registry",
                    config.SOURCE_REGISTRY_DB_PATH.parent,
                )
            )
    except (OSError, RuntimeError, ValueError):
        candidates.append(
            ScanCandidate(None, "source_registry", "source_registry/[UNAVAILABLE]", "candidate_discovery_failed")
        )

    unique: dict[str, ScanCandidate] = {}
    invalid: list[ScanCandidate] = []
    for candidate in candidates:
        if candidate.path is None:
            invalid.append(candidate)
            continue
        key = str(candidate.path).casefold()
        # Project-relative naming is the stable representation for roots that
        # are physically inside the checkout. External configured roots keep
        # their declared root label.
        if key not in unique or candidate.root_kind == "project":
            unique[key] = candidate
    return sorted(
        [*unique.values(), *invalid],
        key=lambda item: (item.relative.casefold(), item.root_kind),
    )


def _git_file_names(*arguments: str) -> set[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=config.PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    return {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/").casefold()
        for item in result.stdout.split(b"\0")
        if item
    }


def _count_text_findings(text: str, relative: str) -> int:
    allowed = {value.casefold() for value in SYNTHETIC_FIXTURE_VALUES.get(relative, set())}
    matches = list(release_secret_matches(text))
    allowed_spans = {
        (match.start, match.end)
        for match in matches
        if text[match.start : match.end].casefold() in allowed
    }
    filtered = [
        match
        for match in matches
        if not any(match.start < end and start < match.end for start, end in allowed_spans)
    ]
    return len(release_secret_groups(filtered))


def _sqlite_occurrences_from_bytes(data: bytes) -> int:
    descriptor, raw_path = tempfile.mkstemp(prefix="security-scan-", suffix=".sqlite")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return _sqlite_occurrences(path)
    finally:
        path.unlink(missing_ok=True)


def _scan_bytes(data: bytes, relative: str) -> tuple[str, int]:
    byte_count = release_secret_count(data)
    if data.startswith(SQLITE_HEADER):
        return "sqlite", max(_sqlite_occurrences_from_bytes(data), byte_count)
    if b"\x00" in data:
        return "binary", byte_count
    try:
        text, _encoding = _decode_text(data)
    except UnicodeError:
        # Binary files are still included in the scan.  We cannot decode them
        # as text, but we can detect ASCII token material and report their
        # type instead of silently dropping the file.
        return "binary", byte_count
    text_count = _count_text_findings(text, relative)
    if relative in SYNTHETIC_FIXTURE_VALUES:
        return "text", text_count
    return "text", max(text_count, byte_count)


def _scan_path(path_or_bytes: Path | bytes, relative: str) -> tuple[str, int]:
    """Analyze supplied snapshot bytes; paths remain a test/backward-compatible input."""
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else path_or_bytes.read_bytes()
    return _scan_bytes(data, relative)


def _snapshot_candidates(paths: list[ScanCandidate | Path]) -> list[ScanSnapshot]:
    snapshots: list[ScanSnapshot] = []
    for raw_candidate in paths:
        candidate = _coerce_candidate(raw_candidate)
        if candidate.path is None:
            snapshots.append(ScanSnapshot(candidate, None, candidate.error_code))
            continue
        try:
            before = _file_identity(candidate.path.stat(follow_symlinks=False))
            data = read_pinned_file_bytes(candidate.path)
            after = _file_identity(candidate.path.stat(follow_symlinks=False))
            if before != after:
                raise RuntimeError("candidate identity changed during snapshot")
            snapshots.append(ScanSnapshot(candidate, data, identity=after))
        except (OSError, RuntimeError) as exc:
            snapshots.append(
                ScanSnapshot(
                    candidate,
                    None,
                    safe_exception_token(
                        "inventory_read_failed",
                        exc,
                        component="security_release_scan",
                    ),
                )
            )
    return snapshots


def _inventory_digest(paths: list[ScanCandidate | ScanSnapshot | Path]) -> str:
    """Hash the physical scan scope so a report cannot outlive file changes."""
    records: list[dict[str, object]] = []
    snapshots: list[ScanSnapshot] = []
    for item in paths:
        if isinstance(item, ScanSnapshot):
            snapshots.append(item)
        else:
            snapshots.extend(_snapshot_candidates([item]))
    for raw_snapshot in snapshots:
        assert isinstance(raw_snapshot, ScanSnapshot)
        candidate = raw_snapshot.candidate
        if raw_snapshot.data is None:
            records.append(
                {
                    "path": candidate.relative,
                    "root_kind": candidate.root_kind,
                    "bytes": None,
                    "sha256": None,
                    "error": raw_snapshot.error or candidate.error_code,
                }
            )
            continue
        data = raw_snapshot.data
        records.append(
            {
                "path": candidate.relative,
                "root_kind": candidate.root_kind,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "identity": list(raw_snapshot.identity or ()),
            }
        )
    records.sort(key=lambda item: str(item["path"]).casefold())
    return hashlib.sha256(canonical_json(records)).hexdigest()


def scan() -> dict:
    findings = []
    errors = []
    scanned = 0
    binary_files = 0
    protected_files: list[dict[str, int | str]] = []
    tracked_protected: set[str] = set()
    staged_protected: set[str] = set()
    git_inspection_errors: list[str] = []
    metadata_by_physical_key: dict[str, int] = {}
    physical_keys_by_safe_path: dict[str, set[str]] = {}

    def safe_path(relative: str, physical_key: str) -> tuple[str, int]:
        safe, count = redact_release_secrets(relative)
        metadata_by_physical_key.setdefault(physical_key, count)
        physical_keys_by_safe_path.setdefault(safe, set()).add(physical_key)
        return safe, count

    def git_observation(relative: str) -> tuple[str, str]:
        candidate = _make_candidate(
            config.PROJECT_ROOT / Path(relative),
            "project",
            config.PROJECT_ROOT,
        )
        physical_key = candidate.physical_key
        if physical_key is None:
            physical_key = "git-invalid:" + hashlib.sha256(
                relative.encode("utf-8", errors="replace")
            ).hexdigest()
        safe, _count = safe_path(relative, physical_key)
        return safe, physical_key

    try:
        tracked = _git_file_names("ls-files", "-z")
        staged = _git_file_names("diff", "--cached", "--name-only", "-z")
        raw_tracked_protected = {
            path for path in tracked if Path(path).name.casefold() in PROTECTED_SECRET_FILES
        }
        raw_staged_protected = {
            path for path in staged if Path(path).name.casefold() in PROTECTED_SECRET_FILES
        }
        tracked_protected = {
            git_observation(path)[0] for path in raw_tracked_protected
        }
        staged_protected = {
            git_observation(path)[0] for path in raw_staged_protected
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        git_inspection_errors.append(
            safe_exception_token(
                "cannot_inspect_git_files",
                exc,
                component="security_release_scan",
            )
        )
    try:
        candidates = _candidate_files()
    except (OSError, RuntimeError, ValueError) as exc:
        candidates = []
        errors.append(
            {
                "path": "[DISCOVERY]",
                "error": safe_exception_token(
                    "candidate_discovery_failed",
                    exc,
                    component="security_release_scan",
                ),
                "error_code": "candidate_discovery_failed",
            }
        )
    snapshots = _snapshot_candidates(candidates)
    inventory_sha256 = _inventory_digest(snapshots)
    for snapshot in snapshots:
        candidate = snapshot.candidate
        relative = candidate.relative
        physical_key = candidate.physical_key or (
            "invalid:" + hashlib.sha256(
                f"{candidate.root_kind}:{relative}".encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()
        )
        safe_relative, path_occurrences = safe_path(relative, physical_key)
        if candidate.path is None or snapshot.data is None:
            errors.append(
                {
                    "path": safe_relative,
                    "error": snapshot.error or "PathBoundaryError",
                    "error_code": candidate.error_code or "candidate_analysis_failed",
                }
            )
            continue
        path = candidate.path
        try:
            file_type, count = _scan_path(snapshot.data, relative)
        except (OSError, UnicodeError, sqlite3.Error, ValueError) as exc:
            errors.append(
                {
                    "path": safe_relative,
                    "error": safe_exception_token(
                        "candidate_analysis_failed",
                        exc,
                        component="security_release_scan",
                    ),
                    "error_code": "candidate_analysis_failed",
                }
            )
            continue
        scanned += 1
        binary_files += file_type == "binary"
        is_protected = path.name.casefold() in PROTECTED_SECRET_FILES
        if is_protected:
            protected_files.append(
                {
                    "path": safe_relative,
                    "occurrences": count + path_occurrences,
                    "_physical_key": physical_key,
                }
            )
        if count or path_occurrences:
            entry = {
                "path": safe_relative,
                "occurrences": count + path_occurrences,
                "metadata_occurrences": path_occurrences,
                "_physical_key": physical_key,
            }
            findings.append(entry)
    try:
        final_candidates = _candidate_files()
        final_snapshots = _snapshot_candidates(final_candidates)
        final_inventory_sha256 = _inventory_digest(final_snapshots)
        if final_inventory_sha256 != inventory_sha256:
            errors.append(
                {
                    "path": "[FRESHNESS]",
                    "error": "candidate_inventory_changed",
                    "error_code": "candidate_inventory_changed",
                }
            )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(
            {
                "path": "[FRESHNESS]",
                "error": safe_exception_token(
                    "candidate_freshness_failed",
                    exc,
                    component="security_release_scan",
                ),
                "error_code": "candidate_freshness_failed",
            }
        )
    colliding_safe_paths = {
        safe
        for safe, physical_keys in physical_keys_by_safe_path.items()
        if len(physical_keys) > 1
    }
    for collection in (protected_files, findings):
        for item in collection:
            physical_key = str(item.pop("_physical_key", ""))
            if item["path"] in colliding_safe_paths:
                item["path_id"] = hashlib.sha256(
                    physical_key.encode("utf-8", errors="replace")
                ).hexdigest()[:16]
    protected_occurrences = sum(int(item["occurrences"]) for item in protected_files)
    metadata_occurrences = sum(metadata_by_physical_key.values())
    status = "passed" if not (
        findings
        or protected_occurrences
        or metadata_occurrences
        or errors
        or tracked_protected
        or staged_protected
        or git_inspection_errors
    ) else "failed"
    return {
        "candidate_files": len(candidates),
        "inventory_sha256": inventory_sha256,
        "scanned_files": scanned,
        "skipped_files": [],
        "excluded_roots": [".git", ".venv", "__pycache__", ".pytest_cache"],
        "binary_files": binary_files,
        "unreadable_files": len(errors),
        "errors": errors,
        "protected_files": protected_files,
        "protected_occurrences": protected_occurrences,
        "metadata_occurrences": metadata_occurrences,
        "tracked_protected_files": sorted(tracked_protected),
        "staged_protected_files": sorted(staged_protected),
        "git_inspection_errors": git_inspection_errors,
        "finding_files": len(findings),
        "occurrences": sum(item["occurrences"] for item in findings),
        "findings": findings,
        "status": status,
    }


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--release-run-id")
    args = parser.parse_args()
    safe_resolve_release_path(args.report, label="security_report_path")
    preflight = None
    if args.manifest is not None:
        if not args.release_run_id:
            parser.error("--release-run-id is required with --manifest")
        validation = load_manifest_file(
            safe_resolve_release_path(args.manifest, label="release_manifest_path")
        )
        preflight = preflight_release_identity(
            args.release_run_id,
            args.manifest,
            validation=validation,
            identity_builder=build_identity,
        )
    report = scan()
    report["schema_version"] = "security_release_scan_v2"
    if preflight is not None:
        identity = preflight.identity
        report = stamp_report(
            report,
            identity,
            f"python scripts/security_release_scan.py --report {args.report} --manifest {args.manifest} --release-run-id {args.release_run_id}",
            producer="scripts.security_release_scan",
            producer_schema_version="security_release_scan_v2",
            source_inputs={
                "manifest_sha256": identity["manifest_sha256"],
                "corpus_identity_sha256": identity["corpus_identity_sha256"],
            },
            command_argv=[
                "python", "scripts/security_release_scan.py", "--report", str(args.report),
                "--manifest", str(args.manifest), "--release-run-id", args.release_run_id,
            ],
        )
    assert_no_release_secrets(canonical_json(report), label="security_release_scan_report")
    atomic_write_release_json(args.report, report, label="security_release_scan_report")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "scanned_files",
                    "finding_files",
                    "occurrences",
                    "protected_occurrences",
                    "metadata_occurrences",
                )
            }
        )
    )
    return 1 if (
        report["status"] != "passed"
        or report["finding_files"]
        or report["unreadable_files"]
        or report["occurrences"]
        or report["protected_occurrences"]
        or report["metadata_occurrences"]
        or report["tracked_protected_files"]
        or report["staged_protected_files"]
        or report["git_inspection_errors"]
    ) else 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="security_release_scan"))
