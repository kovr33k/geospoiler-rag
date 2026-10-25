"""Assemble one immutable, identity-bound release evidence bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    RELEASE_REPORT_KINDS,
    RELEASE_RESERVED_KINDS,
    ReleasePublicationCommittedError,
    SafeArgumentParser,
    assert_clean_release_tree,
    assert_no_release_secrets,
    atomic_write_release_json,
    build_identity,
    canonical_json,
    capture_release_artifact_tree_snapshot,
    durable_prepare_tree,
    evidence_manifest,
    materialize_release_artifact_tree_snapshot,
    pinned_directory,
    preflight_release_identity,
    prepared_publication_tree,
    publication_outcome,
    run_release_cli,
    safe_exception_diagnostic,
    safe_release_exception,
    safe_resolve_release_path,
    scan_release_artifact_tree,
    sha256_file,
    strict_json_loads,
    validate_report_envelope,
)
from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _safe_filename(kind: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", kind).strip("._")
    if not value:
        raise ValueError(f"Invalid evidence kind: {kind!r}")
    return f"{value}.json"


_TEST_LOG_DESTINATIONS = {
    "pytest": "logs/pytest.log",
    "ruff": "logs/ruff.log",
    "compileall": "logs/compileall.log",
    "diff_check": "logs/diff_check.log",
}
_TEST_RUNNER_DESTINATIONS = {
    "runner_receipts_path": ("test_runner_receipts", "logs/runner_receipts.json"),
    "runner_lifecycle_path": ("test_runner_lifecycle", "logs/runner_lifecycle.json"),
}


def _process_start_marker(pid: int) -> float | None:
    """Return a PID-reuse-resistant process marker when psutil is available."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


_LOCK_PAYLOAD_FIELDS = (
    "schema_version",
    "pid",
    "hostname",
    "process_start_time",
    "attempt_name",
    "target_path",
    "created_at",
    "lock_token",
    "status",
    "released_at",
)


def _safe_lock_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {key: payload[key] for key in _LOCK_PAYLOAD_FIELDS if key in payload}


def _write_lock_recovery_receipt(
    lock_path: Path,
    quarantined_path: Path,
    lock_payload: object,
    reason: str,
    error: Exception | None = None,
) -> Path:
    """Record recovery of a lock left by an interrupted bundle writer."""
    recovery_path = lock_path.with_name(
        f"{lock_path.name}.recovery-{os.getpid()}-{os.urandom(4).hex()}.json"
    )
    diagnostic = (
        safe_exception_diagnostic(
            "release_bundle_lock_read_failed", error, component="release_bundle_lock"
        )
        if error is not None
        else None
    )
    atomic_write_release_json(
        recovery_path,
        {
            "schema_version": "release_bundle_recovery_v1",
            "status": "failed",
            "reason": reason,
            "recovered_at": datetime.now(UTC).isoformat(),
            "lock_name_sha256": hashlib.sha256(lock_path.name.encode()).hexdigest(),
            "quarantined_name_sha256": hashlib.sha256(quarantined_path.name.encode()).hexdigest(),
            "lock_payload": _safe_lock_payload(lock_payload),
            "lock_read_error": diagnostic,
        },
        label="release_bundle_lock_recovery",
    )
    return recovery_path


def _lock_descriptor(descriptor: int) -> object:
    """Acquire a non-blocking kernel lock; JSON is diagnostic metadata only."""
    if os.name != "nt":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    overlapped = OVERLAPPED()
    lock_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
    lock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    )
    lock_file_ex.restype = wintypes.BOOL
    flags = 0x2 | 0x1  # LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY
    handle = msvcrt.get_osfhandle(descriptor)
    if not lock_file_ex(handle, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
        raise BlockingIOError(ctypes.get_last_error(), "release lock is held")
    return overlapped


def _unlock_descriptor(descriptor: int, state: object) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    unlock_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).UnlockFileEx
    unlock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    unlock_file_ex.restype = wintypes.BOOL
    unlock_file_ex(
        msvcrt.get_osfhandle(descriptor),
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        ctypes.byref(state),
    )


def _read_lock_descriptor(descriptor: int) -> tuple[dict[str, Any] | None, Exception | None]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if not chunks:
            return None, None
        payload = strict_json_loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("lock JSON root must be an object")
        return payload, None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, exc


def _write_lock_descriptor(descriptor: int, payload: dict[str, Any]) -> None:
    data = canonical_json(payload) + b"\n"
    assert_no_release_secrets(data, label="release_bundle_lock")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("release lock metadata write made no progress")
        view = view[written:]
    os.fsync(descriptor)


@contextmanager
def _held_release_bundle_lock_on_path(
    lock_path: Path, lock_payload: dict[str, Any]
) -> Iterator[None]:
    descriptor: int | None
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        descriptor = None
    if descriptor is None:
        raise FileExistsError("release evidence coordination lock unavailable")
    lock_state: object | None = None
    acquired = False
    try:
        try:
            lock_state = _lock_descriptor(descriptor)
        except (BlockingIOError, OSError):
            raise FileExistsError(f"release evidence writer already active: {lock_path}") from None
        acquired = True
        previous, previous_error = _read_lock_descriptor(descriptor)
        if previous is not None and previous.get("status", "active") == "active":
            _write_lock_recovery_receipt(
                lock_path,
                lock_path,
                previous,
                "interrupted_kernel_lock_recovered",
            )
        elif previous_error is not None:
            _write_lock_recovery_receipt(
                lock_path,
                lock_path,
                None,
                "malformed_lock_metadata_recovered",
                previous_error,
            )
        _write_lock_descriptor(descriptor, {**lock_payload, "status": "active"})
        yield
    finally:
        try:
            if acquired:
                try:
                    _write_lock_descriptor(
                        descriptor,
                        {
                            **lock_payload,
                            "status": "released",
                            "released_at": datetime.now(UTC).isoformat(),
                        },
                    )
                finally:
                    _unlock_descriptor(descriptor, lock_state)
        finally:
            os.close(descriptor)


@contextmanager
def _held_release_bundle_lock(
    lock_path: Path, lock_payload: dict[str, Any]
) -> Iterator[None]:
    with pinned_directory(lock_path.parent) as parent:
        with _held_release_bundle_lock_on_path(parent.child(lock_path.name), lock_payload):
            yield


def _safe_staging_file(value: object, staging_root: Path, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError(f"{label} must be a non-empty path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = safe_resolve_release_path(
        candidate,
        label="release_staging_file",
    )
    try:
        resolved.relative_to(staging_root)
    except ValueError:
        raise ValueError(f"{label} escapes staging root") from None
    current = candidate
    while True:
        if current.is_symlink():
            raise ValueError(f"{label} must not use a symlink")
        if current == current.parent:
            break
        current = current.parent
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular file")
    return resolved


def _build_bundle(
    output_dir: Path,
    manifest_path: Path,
    release_run_id: str,
    reports: list[tuple[str, Path]],
    *,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    output_dir = safe_resolve_release_path(
        output_dir,
        label="release_bundle_output_path",
    )
    if output_dir.exists():
        raise FileExistsError("Evidence output already exists")
    assert_clean_release_tree()
    staging_root = safe_resolve_release_path(
        staging_root or manifest_path.parent,
        label="release_staging_root",
    )
    if not staging_root.is_dir():
        raise ValueError("staging root is not a directory")
    manifest_source = _safe_staging_file(manifest_path, staging_root, "manifest")
    try:
        output_dir.relative_to(staging_root)
    except ValueError:
        pass
    else:
        raise ValueError("bundle output must be outside staging root")
    manifest_validation = load_manifest_file(manifest_source)
    manifest_payload = manifest_validation.payload
    manifest_bytes = (
        manifest_validation.manifest_bytes
        if manifest_validation.manifest_bytes is not None
        else manifest_source.read_bytes()
    )
    if manifest_payload.get("schema_version") != "accepted_manifest_v2":
        raise ValueError("release evidence requires accepted_manifest_v2")
    if manifest_payload.get("run_id") != release_run_id:
        raise ValueError(
            "release_run_id must equal the already-created manifest run_id; "
            "post-hoc manifest rebinding is forbidden"
        )
    provided_kinds = [kind for kind, _source in reports]
    if set(provided_kinds) != set(RELEASE_REPORT_KINDS):
        missing = sorted(set(RELEASE_REPORT_KINDS) - set(provided_kinds))
        extra = sorted(set(provided_kinds) - set(RELEASE_REPORT_KINDS))
        raise ValueError(f"release bundle report kinds must be exact; missing={missing}, extra={extra}")
    if manifest_validation.errors:
        raise ValueError("accepted manifest validation failed")
    manifest_out = output_dir / "accepted_manifest_v2.json"
    identity = build_identity(
        release_run_id,
        manifest_source,
        manifest_payload,
        manifest_bytes=manifest_bytes,
    )
    assert_no_release_secrets(manifest_bytes, label="accepted_manifest")

    prepared_reports: list[tuple[str, bytes]] = []
    auxiliary_files: list[tuple[str, bytes, str]] = []
    records: list[dict[str, Any]] = [
        {
            "kind": "accepted_manifest",
            "path": manifest_out.name,
            "bytes": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    ]
    seen: set[str] = set()
    seen_sources: set[str] = set()
    destination_names = {manifest_out.name, "run_index.json", "evidence_manifest.json"}
    if not re.fullmatch(r"attempt-\d{3}", output_dir.name):
        raise ValueError("release evidence output directory must use canonical attempt-NNN name")
    sanitized = [_safe_filename(kind) for kind in provided_kinds]
    if len(set(sanitized)) != len(sanitized):
        raise ValueError("Evidence report filename collision after sanitization")
    for kind, source in reports:
        if kind in seen or kind in RELEASE_RESERVED_KINDS or kind not in RELEASE_REPORT_KINDS:
            raise ValueError(f"Duplicate/invalid evidence kind: {kind}")
        seen.add(kind)
        source = _safe_staging_file(source, staging_root, f"report[{kind}]")
        source_key = str(source).casefold()
        if source_key in seen_sources:
            raise ValueError(f"Duplicate resolved evidence report path: {source}")
        seen_sources.add(source_key)
        source_bytes = source.read_bytes()
        assert_no_release_secrets(source_bytes, label=f"report:{kind}")
        payload = strict_json_loads(source_bytes)
        if not isinstance(payload, dict):
            raise ValueError(f"Evidence report root must be an object: {source}")
        if payload.get("release_schema_version") != "release_evidence_v1":
            raise ValueError(f"Evidence report is not identity-bound: {source}")
        if payload.get("release_identity") != identity:
            raise ValueError(f"Evidence report identity mismatch: {source}")
        envelope_errors = validate_report_envelope(payload, kind=kind, identity=identity)
        if envelope_errors:
            raise ValueError(f"Evidence report envelope invalid: {source}: {envelope_errors}")
        prepared_reports.append((kind, source_bytes))
        destination = output_dir / _safe_filename(kind)
        if destination.name in destination_names:
            raise ValueError(f"Evidence report filename collision: {kind} -> {destination.name}")
        destination_names.add(destination.name)
        records.append(
            {
                "kind": kind,
                "path": destination.name,
                "bytes": len(source_bytes),
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
            }
        )
        if kind == "tests":
            checks = payload.get("checks")
            if not isinstance(checks, list):
                raise ValueError("test evidence checks must be a list")
            seen_check_names: set[str] = set()
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("name"), str):
                    raise ValueError("test evidence check is invalid")
                name = check["name"]
                if name not in _TEST_LOG_DESTINATIONS:
                    raise ValueError(f"unknown test check name: {name}")
                if name in seen_check_names:
                    raise ValueError(f"duplicate test check name: {name}")
                seen_check_names.add(name)
                log_source = _safe_staging_file(check.get("log_path"), staging_root, f"test log[{name}]")
                log_bytes = log_source.read_bytes()
                assert_no_release_secrets(log_bytes, label=f"test_log:{name}")
                if check.get("log_bytes") != len(log_bytes) or check.get("log_sha256") != hashlib.sha256(log_bytes).hexdigest():
                    raise ValueError(f"test log receipt mismatch: {name}")
                aux_kind = f"test_log_{name}"
                aux_relative = _TEST_LOG_DESTINATIONS[name]
                auxiliary_files.append((aux_kind, log_bytes, aux_relative))
            if seen_check_names != set(_TEST_LOG_DESTINATIONS):
                raise ValueError("test evidence check names must be exact")
            summary_source = _safe_staging_file(payload.get("summary_path"), staging_root, "test summary")
            summary_bytes = summary_source.read_bytes()
            assert_no_release_secrets(summary_bytes, label="test_summary")
            if payload.get("summary_sha256") != hashlib.sha256(summary_bytes).hexdigest():
                raise ValueError("test summary receipt mismatch")
            auxiliary_files.append(("test_summary", summary_bytes, "logs/test_summary.json"))
            for field, (aux_kind, aux_relative) in _TEST_RUNNER_DESTINATIONS.items():
                runner_source = _safe_staging_file(payload.get(field), staging_root, f"test {field}")
                hash_field = field.replace("_path", "_sha256")
                runner_bytes = runner_source.read_bytes()
                assert_no_release_secrets(runner_bytes, label=aux_kind)
                if payload.get(hash_field) != hashlib.sha256(runner_bytes).hexdigest():
                    raise ValueError(f"test runner receipt mismatch: {field}")
                auxiliary_files.append((aux_kind, runner_bytes, aux_relative))
        elif kind == "late_fusion_smoke":
            ledger_source = _safe_staging_file(payload.get("attempt_history_path"), staging_root, "smoke attempt ledger")
            ledger_bytes = ledger_source.read_bytes()
            assert_no_release_secrets(ledger_bytes, label="smoke_attempt_ledger")
            auxiliary_files.append(("smoke_attempt_ledger", ledger_bytes, "logs/smoke_attempts.json"))
        elif kind == "backup":
            backup_manifest = _safe_staging_file(
                payload.get("manifest"), staging_root, "backup source manifest"
            )
            backup_manifest_bytes = backup_manifest.read_bytes()
            assert_no_release_secrets(
                backup_manifest_bytes, label="backup_source_manifest"
            )
            if payload.get("backup_manifest_sha256") != hashlib.sha256(
                backup_manifest_bytes
            ).hexdigest():
                raise ValueError("backup source manifest receipt mismatch")
            auxiliary_files.append(
                (
                    "backup_source_manifest",
                    backup_manifest_bytes,
                    "backup/source_manifest.json",
                )
            )

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=parent))
    bundle_error = None
    try:
        manifest_out = temporary_dir / manifest_out.name
        manifest_out.write_bytes(manifest_bytes)
        for kind, source_bytes in prepared_reports:
            (temporary_dir / _safe_filename(kind)).write_bytes(source_bytes)
        for aux_kind, source_bytes, relative in auxiliary_files:
            destination = temporary_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as destination_handle:
                destination_handle.write(source_bytes)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            data_bytes = destination.stat().st_size
            records.append(
                {
                    "kind": aux_kind,
                    "path": relative,
                    "bytes": data_bytes,
                    "sha256": sha256_file(destination),
                }
            )
        run_index = {
            "schema_version": "release_run_index_v1",
            "release_run_id": release_run_id,
            "attempt_name": output_dir.name,
            "bundle_path": ".",
            "manifest_sha256": identity["manifest_sha256"],
            "report_kinds": sorted(seen),
        }
        run_index_bytes = canonical_json(run_index) + bytes([10])
        (temporary_dir / "run_index.json").write_bytes(run_index_bytes)
        records.append(
            {
                "kind": "run_index",
                "path": "run_index.json",
                "bytes": len(run_index_bytes),
                "sha256": hashlib.sha256(run_index_bytes).hexdigest(),
            }
        )
        bundle = evidence_manifest(identity, records)
        (temporary_dir / "evidence_manifest.json").write_bytes(canonical_json(bundle) + b"\n")
        with prepared_publication_tree(temporary_dir, output_dir) as publication:
            prepared_dir = publication.io_path
            durable_prepare_tree(prepared_dir)
            analyzed_snapshot = capture_release_artifact_tree_snapshot(prepared_dir)
            bundle_scan = scan_release_artifact_tree(
                prepared_dir, analyzed_snapshot=analyzed_snapshot
            )
            if bundle_scan.get("status") != "passed":
                raise ValueError("bundle secret scan failed")
            # The producer must be able to read the exact pinned bundle it emitted.
            from geospoiler.operations.build_release_verification import _evidence_bundle  # noqa: PLC0415

            preflight_errors: list[str] = []
            with tempfile.TemporaryDirectory(
                prefix="release-bundle-validation-", dir=output_dir.parent
            ) as validation_raw:
                validation_dir = Path(validation_raw)
                materialize_release_artifact_tree_snapshot(
                    analyzed_snapshot, validation_dir
                )
                _identity, preflight_paths = _evidence_bundle(
                    validation_dir, preflight_errors
                )
            required_paths = {
                "accepted_manifest",
                "backup_source_manifest",
                "run_index",
                *RELEASE_REPORT_KINDS,
            }
            extras = set(preflight_paths) - required_paths
            if preflight_errors or not required_paths.issubset(preflight_paths) or not all(
                kind.startswith("test_log_")
                or kind in {"test_summary", "test_runner_receipts", "test_runner_lifecycle", "smoke_attempt_ledger"}
                for kind in extras
            ):
                raise ValueError(f"bundle self-preflight failed: {preflight_errors}")
            publication.seal(str(bundle_scan["inventory_sha256"]))
            publication.commit()
    except Exception as exc:
        committed = isinstance(exc, ReleasePublicationCommittedError)
        if not committed:
            shutil.rmtree(temporary_dir, ignore_errors=True)
        suffix = "committed-uncertain" if committed else "failed"
        failure_path = output_dir.parent / f"{output_dir.name}.{suffix}-{os.getpid()}-{os.urandom(4).hex()}.json"
        try:
            diagnostic = safe_exception_diagnostic(
                (
                    "release_bundle_publication_committed_uncertain"
                    if committed
                    else "release_bundle_publication_failed"
                ),
                exc,
                component="release_bundle",
            )
            atomic_write_release_json(
                failure_path,
                {
                    "schema_version": "release_bundle_failure_v1",
                    **publication_outcome(exc),
                    "error": "bundle self-preflight or publication failed",
                    **diagnostic,
                },
                label="release_bundle_failure",
            )
        except Exception:
            pass
        bundle_error = (
            exc
            if committed
            else safe_release_exception(
                "release_bundle_publication_failed",
                exc,
                component="release_bundle",
            )
        )
    if bundle_error is not None:
        raise bundle_error from None
    return {
        "status": "passed",
        "release_run_id": release_run_id,
        "output_dir": str(output_dir),
        "reports": len(records),
        "manifest_sha256": identity["manifest_sha256"],
    }


def build_bundle(
    output_dir: Path,
    manifest_path: Path,
    release_run_id: str,
    reports: list[tuple[str, Path]],
    *,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    """Build one attempt while holding an exclusive sibling lock."""
    output_dir = safe_resolve_release_path(
        output_dir,
        label="release_bundle_output_path",
    )
    provided_kinds = {kind for kind, _path in reports}
    if provided_kinds == set(RELEASE_REPORT_KINDS):
        assert_clean_release_tree()
        validation = load_manifest_file(
            safe_resolve_release_path(
                manifest_path,
                label="release_manifest_path",
            )
        )
        preflight_release_identity(
            release_run_id,
            manifest_path,
            validation=validation,
            identity_builder=build_identity,
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir.parent / f".{output_dir.name}.lock"
    lock_payload = {
        "schema_version": "release_bundle_lock_v2",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_start_time": _process_start_marker(os.getpid()),
        "attempt_name": output_dir.name,
        "target_path": str(output_dir),
        "created_at": datetime.now(UTC).isoformat(),
        "lock_token": os.urandom(16).hex(),
    }
    with _held_release_bundle_lock(lock_path, lock_payload):
        return _build_bundle(
            output_dir,
            manifest_path,
            release_run_id,
            reports,
            staging_root=staging_root,
        )


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help="Report source; repeat for each release evidence kind.",
    )
    args = parser.parse_args()
    parsed: list[tuple[str, Path]] = []
    for value in args.report:
        kind, separator, path = value.partition("=")
        if not separator or not kind or not path:
            parser.error("--report must be KIND=PATH")
        parsed.append((kind, Path(path)))
    result = build_bundle(args.output_dir, args.manifest, args.release_run_id, parsed, staging_root=args.staging_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="build_release_evidence"))
