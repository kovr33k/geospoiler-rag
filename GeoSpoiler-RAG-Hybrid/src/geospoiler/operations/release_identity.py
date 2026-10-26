"""Shared release-evidence identity and hashing helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata as importlib_metadata
import io
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import threading
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from geospoiler import config  # noqa: E402
from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.providers import llm_backend  # noqa: E402

RELEASE_EVIDENCE_SCHEMA = "release_evidence_v1"
MEDIA_CONTRACT_VERSION = "media-contract-v2"
COLD_GRAPH_BATCH_SIZE_DEFAULT = 1
COLD_GRAPH_BATCH_SIZE_MAX = 25
COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT = 1800.0
STATIC_ENVIRONMENT_PACKAGE_NAMES = (
    "telethon",
    "lightrag-hku",
    "openai",
    "crawl4ai",
    "streamlit",
    "requests",
    "yt-dlp",
    "python-dotenv",
    "pydantic",
    "aiofiles",
    "Pillow",
    "qrcode",
    "pypdf",
    "lingua-language-detector",
    "pre-commit",
    "pytest",
    "ruff",
)
STATIC_ENVIRONMENT_COMMAND_NAMES = ("python", "pytest", "ruff")
DEPENDENCY_SPEC_RELATIVE_PATHS = ("pyproject.toml", "requirements.txt")
TRUSTED_RELEASE_BASE_REF = "origin/main"
RELEASE_TEST_TIMEOUT_SECONDS = 1800.0


@dataclass(frozen=True)
class ReleaseProducerSpec:
    """Checked-in description of one official release artifact producer."""

    path: str
    component: str
    schemas: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    cli: bool = True
    cli_probe_args: tuple[str, ...] = ()


RELEASE_PRODUCER_REGISTRY = (
    ReleaseProducerSpec(
        "loader/ingest.py",
        "loader.ingest",
        ("accepted_manifest_load_report_v1",),
        ("loader/manifest_contract.py", "scripts/release_identity.py"),
        cli=False,
    ),
    ReleaseProducerSpec(
        "scripts/build_release_evidence.py",
        "build_release_evidence",
        (
            "release_evidence_v1",
            "release_bundle_failure_v1",
            "release_bundle_lock_v2",
            "release_bundle_recovery_v1",
            "release_run_index_v1",
        ),
        ("loader/manifest_contract.py", "scripts/build_release_verification.py", "scripts/release_identity.py"),
        cli_probe_args=("--output-dir", "{root}/bundle", "--manifest", "{manifest}", "--release-run-id", "run-1", "--staging-root", "{root}/staging"),
    ),
    ReleaseProducerSpec(
        "scripts/build_release_manifest.py",
        "build_release_manifest",
        ("accepted_manifest_v2",),
        ("loader/manifest_contract.py", "scripts/release_identity.py"),
        cli_probe_args=("{report}",),
    ),
    ReleaseProducerSpec("scripts/build_release_risk_register.py", "build_release_risk_register", ("release_risk_register_v1",), ("scripts/release_identity.py",), cli_probe_args=("--input", "{root}/input.json", "--output", "{report}", "--manifest", "{manifest}", "--release-run-id", "run-1")),
    ReleaseProducerSpec("scripts/build_release_test_evidence.py", "build_release_test_evidence", ("release_test_gate_v1", "release_test_runner_lifecycle_v1", "release_test_runner_receipts_v1"), ("scripts/release_identity.py",), cli_probe_args=("--output", "{report}", "--base-ref", TRUSTED_RELEASE_BASE_REF)),
    ReleaseProducerSpec(
        "scripts/build_release_verification.py",
        "build_release_verification",
        (
            "release_verification_v2",
            "release_verification_manifest_v1",
            "release_verification_failure_v1",
            "release_verification_failure_manifest_v1",
        ),
        (
            "loader/manifest_contract.py",
            "scripts/build_release_risk_register.py",
            "scripts/cold_provenance.py",
            "scripts/graph_provenance_reconcile.py",
            "scripts/migrate_portable_paths.py",
            "scripts/release_identity.py",
            "scripts/security_release_scan.py",
            "scripts/smoke_contract.py",
            "scripts/validate_backup_restore.py",
        ),
        cli_probe_args=("--input-dir", "{root}/input", "--run-dir", "{root}/attempt-001", "--release-run-id", "run-1"),
    ),
    ReleaseProducerSpec("scripts/cold_graph_check.py", "cold_graph_check", ("cold_graph_extraction_v1",), ("loader/ingest.py", "loader/manifest_contract.py", "scripts/cold_provenance.py", "scripts/release_identity.py"), cli_probe_args=("--workspace", "{root}/workspace", "--manifest", "{manifest}", "--report", "{report}")),
    ReleaseProducerSpec("scripts/graph_provenance_reconcile.py", "graph_provenance_reconcile", ("graph_provenance_reconciliation_v1",), ("scripts/release_identity.py",), cli_probe_args=("--report", "{report}")),
    ReleaseProducerSpec("scripts/late_fusion_activation_smoke.py", "late_fusion_activation_smoke", ("late_fusion_smoke_v1",), ("loader/manifest_contract.py", "scripts/release_identity.py", "scripts/smoke_contract.py"), cli_probe_args=("--output", "{report}")),
    ReleaseProducerSpec("scripts/luna_reenrich_audit.py", "luna_reenrich_audit", ("luna_reenrich_audit_v1",), ("scripts/release_identity.py",), cli_probe_args=("--run-id", "run-1")),
    ReleaseProducerSpec("scripts/luna_reenrich_control.py", "luna_reenrich_control", ("backup_source_manifest_v1",), ("scripts/release_identity.py",), cli_probe_args=("prepare", "--run-dir", "{root}/luna-run")),
    ReleaseProducerSpec("scripts/migrate_portable_paths.py", "migrate_portable_paths", ("portable_path_migration_v1",), ("scripts/release_identity.py",), cli_probe_args=("scan", "--report", "{report}")),
    ReleaseProducerSpec("scripts/security_artifact_redaction.py", "security_artifact_redaction", ("security_artifact_inventory_v1", "security_artifact_redaction_report_v1", "security_quarantine_manifest_v1"), ("scripts/release_identity.py",), cli_probe_args=("scan", "--root", "{root}/artifacts", "--report-dir", "{root}/reports")),
    ReleaseProducerSpec("scripts/security_release_scan.py", "security_release_scan", ("security_release_scan_v2",), ("loader/manifest_contract.py", "scripts/release_identity.py", "scripts/security_artifact_redaction.py"), cli_probe_args=("--report", "{report}")),
    ReleaseProducerSpec("scripts/sync_media_contract.py", "sync_media_contract", ("media_contract_sync_v1",), ("loader/manifest_contract.py", "scripts/release_identity.py"), cli_probe_args=("--report", "{report}")),
    ReleaseProducerSpec("scripts/validate_backup_restore.py", "validate_backup_restore", ("backup_restore_closeout_v1",), ("scripts/release_identity.py",), cli_probe_args=("--manifest", "{root}/backup.json", "--report", "{report}")),
)
def release_implementation_path(relative: str) -> str:
    """Resolve source ownership while retaining the stable script CLI address."""
    if relative.startswith("scripts/"):
        return "src/geospoiler/operations/" + relative.removeprefix("scripts/")
    if relative in {"loader/ingest.py", "loader/manifest_contract.py"}:
        return "src/geospoiler/retrieval/lightrag/" + relative.removeprefix("loader/")
    return relative


RELEASE_PRODUCER_REGISTRY = tuple(
    replace(spec, dependencies=tuple(dict.fromkeys((
        *spec.dependencies,
        release_implementation_path(spec.path),
        *(release_implementation_path(path) for path in spec.dependencies),
    ))))
    for spec in RELEASE_PRODUCER_REGISTRY
)

RELEASE_EXCEPTION_BOUNDARY_FILES = tuple(
    dict.fromkeys(
        (
            "scripts/release_identity.py",
            *(spec.path for spec in RELEASE_PRODUCER_REGISTRY),
            *(dependency for spec in RELEASE_PRODUCER_REGISTRY for dependency in spec.dependencies),
        )
    )
)


class ReleaseEvidenceError(RuntimeError):
    """A public release error whose text is safe for CLI tracebacks."""


class ReleasePublicationCommittedError(ReleaseEvidenceError):
    """Publication reached os.replace, but a later durability step failed."""

    committed = True


_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


@dataclass
class PinnedDirectory:
    """A directory reached without following replaceable path components."""

    lexical_path: Path
    io_path: Path
    directory_fd: int | None = None
    windows_handles: tuple[str, ...] = ()

    def child(self, name: str) -> Path:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("pinned directory child must be one path component")
        return self.io_path / name


@dataclass
class PreparedPublicationTree:
    """One pinned source tree that is validated and committed as the same object."""

    source: Path
    target: Path
    io_path: Path
    parent: PinnedDirectory
    source_identity: tuple[int, int, int]
    windows_handle: int | None = None
    source_fd: int | None = None
    sealed_inventory_sha256: str | None = None
    committed: bool = False

    def _raise_committed_mismatch(self, message: str) -> NoReturn:
        """Remove an unverified canonical name before reporting committed state."""
        quarantine = self.target.with_name(
            f".{self.target.name}.identity-mismatch-{os.getpid()}-{os.urandom(4).hex()}"
        )
        details: dict[str, Any]
        try:
            if os.name == "nt":
                assert self.windows_handle is not None
                _windows_rename_open_directory(self.windows_handle, quarantine)
                _windows_flush_handle(self.windows_handle)
            else:
                assert self.parent.directory_fd is not None
                _posix_rename_noreplace(
                    self.parent.directory_fd,
                    self.target.name,
                    quarantine.name,
                )
                os.fsync(self.parent.directory_fd)
            details = {
                "recovery_action": "committed_publication_quarantined",
                "quarantine_path": str(quarantine),
            }
        except Exception:
            details = {
                "recovery_action": "committed_publication_recovery_required",
            }
        error = ReleasePublicationCommittedError(message)
        error.publication_details = details
        raise error

    def seal(self, expected_inventory_sha256: str | None = None) -> str:
        """Bind commit to the exact inventory already accepted by preflight."""
        if self.committed:
            raise RuntimeError("prepared publication already committed")
        current_inventory_sha256 = _artifact_tree_inventory_sha256(self.io_path)
        if (
            expected_inventory_sha256 is not None
            and current_inventory_sha256 != expected_inventory_sha256
        ):
            raise RuntimeError("release publication tree changed after validation")
        self.sealed_inventory_sha256 = current_inventory_sha256
        return self.sealed_inventory_sha256

    def commit(self) -> None:
        if self.committed:
            raise RuntimeError("prepared publication already committed")
        if self.sealed_inventory_sha256 is None:
            raise RuntimeError("prepared publication tree is not sealed")
        if _artifact_tree_inventory_sha256(self.io_path) != self.sealed_inventory_sha256:
            raise RuntimeError("release publication tree changed after validation")
        if self.target.exists():
            raise FileExistsError("release publication target already exists")
        committed = False
        try:
            if os.name == "nt":
                assert self.windows_handle is not None
                current = self.source.stat(follow_symlinks=False)
                if _stat_identity(current) != self.source_identity:
                    raise RuntimeError("release publication source identity changed")
                _windows_rename_open_directory(self.windows_handle, self.target)
                committed = True
                self.committed = True
                _windows_flush_handle(self.windows_handle)
                committed_metadata = self.target.stat(follow_symlinks=False)
                if _stat_identity(committed_metadata) != self.source_identity:
                    self._raise_committed_mismatch(
                        "release_publication_committed_source_identity_mismatch"
                    )
            else:
                assert self.source_fd is not None
                assert self.parent.directory_fd is not None
                current = os.stat(
                    self.source.name,
                    dir_fd=self.parent.directory_fd,
                    follow_symlinks=False,
                )
                if _stat_identity(current) != self.source_identity:
                    raise RuntimeError("release publication source identity changed")
                _posix_rename_noreplace(
                    self.parent.directory_fd,
                    self.source.name,
                    self.target.name,
                )
                committed = True
                self.committed = True
                os.fsync(self.parent.directory_fd)
                committed_metadata = os.stat(
                    self.target.name,
                    dir_fd=self.parent.directory_fd,
                    follow_symlinks=False,
                )
                if not (
                    _stat_identity(committed_metadata)
                    == _stat_identity(os.fstat(self.source_fd))
                    == self.source_identity
                ):
                    self._raise_committed_mismatch(
                        "release_publication_committed_source_identity_mismatch"
                    )
            if _artifact_tree_inventory_sha256(self.target) != self.sealed_inventory_sha256:
                self._raise_committed_mismatch(
                    "release_publication_committed_inventory_mismatch"
                )
        except ReleasePublicationCommittedError:
            raise
        except Exception:
            if committed:
                raise ReleasePublicationCommittedError(
                    "release_publication_committed_durability_uncertain"
                ) from None
            raise
        self.committed = True


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mode))


def _path_is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


_WINDOWS_PIN_CACHE: dict[str, tuple[int, int]] = {}
_WINDOWS_PIN_CACHE_LOCK = threading.RLock()


def _windows_pin_directory_chain(path: Path) -> tuple[str, ...]:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    share_read_write = 0x1 | 0x2
    delete_and_read_attributes = 0x00010000 | 0x80
    open_existing = 3
    backup_semantics_open_reparse = 0x02000000 | 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    keys: list[str] = []
    with _WINDOWS_PIN_CACHE_LOCK:
        try:
            for index, component in enumerate(components):
                key = str(component).casefold()
                cached = _WINDOWS_PIN_CACHE.get(key)
                if cached is not None:
                    handle, count = cached
                    _WINDOWS_PIN_CACHE[key] = (handle, count + 1)
                    keys.append(key)
                    continue
                handle = create_file(
                    str(component),
                    delete_and_read_attributes if index == len(components) - 1 else 0x80,
                    share_read_write,
                    None,
                    open_existing,
                    backup_semantics_open_reparse,
                    None,
                )
                if handle == invalid_handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                if _path_is_link_or_reparse(component) or not component.is_dir():
                    close_handle(handle)
                    raise RuntimeError("release path boundary violation")
                _WINDOWS_PIN_CACHE[key] = (int(handle), 1)
                keys.append(key)
        except Exception:
            _windows_close_handles(tuple(keys))
            raise
    return tuple(keys)


def _windows_close_handles(keys: tuple[str, ...]) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    with _WINDOWS_PIN_CACHE_LOCK:
        for key in reversed(keys):
            handle, count = _WINDOWS_PIN_CACHE[key]
            if count == 1:
                del _WINDOWS_PIN_CACHE[key]
                close_handle(handle)
            else:
                _WINDOWS_PIN_CACHE[key] = (handle, count - 1)


def _posix_pin_directory_chain(path: Path) -> tuple[list[int], int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    parts = path.parts[1:]
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        if path.is_relative_to(base):
            suffix = path.relative_to(base).parts
            if suffix and _is_open_directory_descriptor_alias(base / suffix[0]):
                descriptor = os.dup(int(suffix[0]))
                parts = suffix[1:]
                break
    else:
        descriptor = os.open(path.anchor or os.sep, flags | nofollow)
    descriptors.append(descriptor)
    try:
        for part in parts:
            descriptor = os.open(part, flags | nofollow, dir_fd=descriptor)
            descriptors.append(descriptor)
    except Exception:
        for item in reversed(descriptors):
            os.close(item)
        raise
    return descriptors, descriptor


@contextlib.contextmanager
def pinned_directory(path: Path) -> Iterator[PinnedDirectory]:
    """Pin a directory identity; fail closed instead of using unsafe path I/O."""
    lexical = path.absolute()
    if os.name == "nt":
        handles = _windows_pin_directory_chain(lexical)
        try:
            yield PinnedDirectory(lexical, lexical, windows_handles=handles)
        finally:
            _windows_close_handles(handles)
        return
    descriptors, descriptor = _posix_pin_directory_chain(lexical)
    alias: Path | None = None
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = base / str(descriptor)
        if candidate.is_dir():
            alias = candidate
            break
    if alias is None:
        for item in reversed(descriptors):
            os.close(item)
        raise RuntimeError("descriptor-relative directory alias unavailable")
    try:
        yield PinnedDirectory(lexical, alias, directory_fd=descriptor)
    finally:
        for item in reversed(descriptors):
            os.close(item)


@contextlib.contextmanager
def prepared_publication_tree(
    source: Path, target: Path
) -> Iterator[PreparedPublicationTree]:
    """Pin a source directory before any validation and publish that exact object."""
    source = source.absolute()
    target = target.absolute()
    if source.parent != target.parent:
        raise ValueError("prepared publication requires sibling paths")
    with pinned_directory(target.parent) as parent:
        if target.exists():
            raise FileExistsError("release publication target already exists")
        windows_handle: int | None = None
        source_fd: int | None = None
        publication: PreparedPublicationTree | None = None
        try:
            if os.name == "nt":
                windows_handle = _windows_open_directory_for_publication(source)
                io_path = source
                metadata = source.stat(follow_symlinks=False)
            else:
                assert parent.directory_fd is not None
                source_fd = os.open(
                    source.name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent.directory_fd,
                )
                io_path = _descriptor_directory_alias(source_fd)
                metadata = os.fstat(source_fd)
            if os.name == "nt" and (
                _path_is_link_or_reparse(io_path) or not io_path.is_dir()
            ):
                raise RuntimeError("release publication source boundary violation")
            if os.name != "nt" and not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("release publication source boundary violation")
            publication = PreparedPublicationTree(
                source=source,
                target=target,
                io_path=io_path,
                parent=parent,
                source_identity=_stat_identity(metadata),
                windows_handle=windows_handle,
                source_fd=source_fd,
            )
            yield publication
        finally:
            if windows_handle is not None:
                try:
                    _windows_close_handle(windows_handle)
                except Exception:
                    if publication is not None and publication.committed:
                        raise ReleasePublicationCommittedError(
                            "release_publication_committed_durability_uncertain"
                        ) from None
                    raise
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except Exception:
                    if publication is not None and publication.committed:
                        raise ReleasePublicationCommittedError(
                            "release_publication_committed_durability_uncertain"
                        ) from None
                    raise


def _descriptor_directory_alias(descriptor: int) -> Path:
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = base / str(descriptor)
        if candidate.is_dir():
            return candidate
    raise RuntimeError("descriptor-relative directory alias unavailable")


def _is_open_directory_descriptor_alias(path: Path) -> bool:
    normalized = path.as_posix().rstrip("/")
    for prefix in ("/proc/self/fd/", "/dev/fd/"):
        if not normalized.startswith(prefix):
            continue
        suffix = normalized[len(prefix) :]
        if not suffix.isdigit() or "/" in suffix:
            return False
        try:
            return stat.S_ISDIR(os.fstat(int(suffix)).st_mode)
        except OSError:
            return False
    return False


def _is_safe_directory_root(path: Path) -> bool:
    return _is_open_directory_descriptor_alias(path) or (
        not _path_is_link_or_reparse(path) and path.is_dir()
    )


def durable_prepare_tree(directory: Path) -> None:
    """Flush every file and, on POSIX, every directory entry bottom-up."""
    directory = directory.absolute()
    if not _is_safe_directory_root(directory):
        raise RuntimeError("durable tree root boundary violation")
    entries = sorted(directory.rglob("*"))
    for path in entries:
        if _path_is_link_or_reparse(path):
            raise RuntimeError("durable tree contains a link or reparse point")
    directories = [directory, *(item for item in entries if item.is_dir())]
    for path in (item for item in entries if item.is_file()):
        with path.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    if os.name != "nt":
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def read_pinned_file_bytes(path: Path) -> bytes:
    """Read one regular, non-link child while its parent identity is pinned."""
    path = path.absolute()
    with pinned_directory(path.parent) as parent:
        candidate = parent.child(path.name)
        if _path_is_link_or_reparse(candidate) or not candidate.is_file():
            raise RuntimeError("release file boundary violation")
        before = candidate.stat(follow_symlinks=False)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if os.name != "nt" and parent.directory_fd is not None:
            descriptor = os.open(path.name, flags, dir_fd=parent.directory_fd)
        else:
            descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = candidate.stat(follow_symlinks=False)
        if not (
            _stable_stat_identity(before)
            == _stable_stat_identity(opened)
            == _stable_stat_identity(after)
        ):
            raise RuntimeError("release file identity changed during read")
        return data


class ReleaseArgumentParserError(SystemExit):
    """Raised for a rejected release CLI argument without retaining raw argv."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser that never writes an untrusted parse error to stderr."""

    def error(self, message: str) -> None:
        del message
        raise ReleaseArgumentParserError("release_cli_argument_rejected") from None

_RELEASE_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openrouter_token", re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{20,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:access[-_]?token|api[-_]?key|authorization)\b"
            r"\s*[:=]\s*[\"']?(?!\[redacted\]|%5bredacted%5d)"
            r"[^\s,}\"'&<>]{8,}"
        ),
    ),
    (
        "sensitive_query_value",
        re.compile(
            r"(?i)[?&](?:access[-_]?token|api[-_]?key|token|signature|sig)="
            r"(?!\[redacted\]|%5bredacted%5d)[^&\s\"'<>]{8,}"
        ),
    ),
)


@dataclass(frozen=True)
class ReleaseSecretMatch:
    """Safe secret match metadata. The matched value is deliberately omitted."""

    category: str
    start: int
    end: int
    encoding: str


@dataclass(frozen=True)
class ReleaseSecretFinding:
    """One physical secret occurrence, possibly matched by several patterns."""

    start: int
    end: int
    categories: tuple[str, ...]
    encodings: tuple[str, ...]


@dataclass(frozen=True)
class _ReleaseSecretText:
    text: str
    encoding: str
    byte_offset: int = 0

    def physical_span(self, start: int, end: int) -> tuple[int, int]:
        if self.encoding == "unicode":
            return start, end
        if self.encoding == "binary-latin-1":
            return self.byte_offset + start, self.byte_offset + end
        codec = {
            "utf-8": "utf-8",
            "utf-8-sig": "utf-8",
            "utf-16-le": "utf-16-le",
            "utf-16-be": "utf-16-be",
        }[self.encoding]
        return (
            self.byte_offset + len(self.text[:start].encode(codec)),
            self.byte_offset + len(self.text[:end].encode(codec)),
        )


def _expected_environment_command_argv(name: str) -> list[str]:
    suffixes = {
        "python": ["--version"],
        "pytest": ["-m", "pytest", "--version"],
        "ruff": ["-m", "ruff", "--version"],
    }
    suffix = suffixes.get(name)
    if suffix is None:
        raise ValueError(f"unknown static environment command: {name}")
    return [sys.executable, *suffix]


def _environment_argv_matches(actual: Any, expected: list[str]) -> bool:
    return isinstance(actual, list) and actual == expected


def normalise_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def current_static_environment_snapshot() -> dict[str, Any]:
    """Read the versions installed in this interpreter without network access."""
    errors: list[str] = []
    packages: dict[str, str | None] = {}
    canonical_names: dict[str, str] = {}
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        InvalidVersion = None  # type: ignore[assignment]
        Version = None  # type: ignore[assignment]
        errors.append("environment_version_validator_unavailable")

    for package in STATIC_ENVIRONMENT_PACKAGE_NAMES:
        canonical = normalise_distribution_name(package)
        canonical_names[canonical] = package
        try:
            raw_version = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            packages[package] = None
            errors.append(f"environment_installed_package_missing:{package}")
            continue
        except Exception as exc:  # pragma: no cover - defensive metadata boundary
            packages[package] = None
            diagnostic = safe_exception_diagnostic(
                "environment_installed_package_error",
                exc,
                component="release_environment",
            )
            errors.append(
                ":".join(
                    (
                        diagnostic["error_code"],
                        package,
                        diagnostic["exception_type"],
                        diagnostic["message_sha256"],
                    )
                )
            )
            continue
        if not isinstance(raw_version, str) or not raw_version.strip():
            packages[package] = None
            errors.append(f"environment_installed_version_missing:{package}")
            continue
        version = raw_version.strip()
        packages[package] = version
        if Version is not None and InvalidVersion is not None:
            try:
                Version(version)
            except InvalidVersion:
                errors.append(f"environment_installed_version_invalid:{package}")

    python_version = platform.python_version()
    package_versions = {
        name: packages.get(name)
        for name in STATIC_ENVIRONMENT_PACKAGE_NAMES
    }
    command_versions = {
        "python": f"Python {python_version}",
        "pytest": (
            f"pytest {package_versions['pytest']}"
            if package_versions["pytest"]
            else None
        ),
        "ruff": (
            f"ruff {package_versions['ruff']}"
            if package_versions["ruff"]
            else None
        ),
    }
    return {
        "python_version": python_version,
        "packages": package_versions,
        "canonical_package_names": sorted(canonical_names),
        "command_versions": command_versions,
        "errors": sorted(set(errors)),
    }


def dependency_spec_receipt(project_root: Path | None = None) -> dict[str, Any]:
    """Read dependency specs and bind their exact bytes to one identity."""
    root = (project_root or PROJECT_ROOT).resolve()
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    identity = hashlib.sha256()
    for relative in DEPENDENCY_SPEC_RELATIVE_PATHS:
        path = root / relative
        try:
            data = path.read_bytes()
        except (OSError, UnicodeError):
            errors.append(f"dependency_file_unreadable:{relative}")
            files.append({"path": relative, "bytes": None, "sha256": None})
            continue
        path_bytes = relative.encode("utf-8")
        identity.update(len(path_bytes).to_bytes(8, "big"))
        identity.update(path_bytes)
        identity.update(len(data).to_bytes(8, "big"))
        identity.update(data)
        files.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
    return {
        "files": files,
        "identity_sha256": None if errors else identity.hexdigest(),
        "errors": errors,
    }

# One registry is shared by producers, the bundle builder and the final
# verifier.  It describes the payload as well as the envelope: merely
# re-stamping ``{"status": "passed"}`` is not release evidence.
def _contract(
    producer: str,
    schema_version: str,
    required_payload: set[str],
    allowed_payload: set[str],
    field_types: dict[str, str],
) -> dict[str, Any]:
    return {
        "producer": producer,
        "schema_version": schema_version,
        "required_payload": frozenset(required_payload),
        "allowed_payload": frozenset(allowed_payload),
        "field_types": dict(field_types),
    }


REPORT_CONTRACTS: dict[str, dict[str, Any]] = {
    "manifest_load": _contract(
        "loader.ingest",
        "accepted_manifest_load_report_v1",
        {"schema_version", "run_id", "manifest_path", "manifest_sha256", "status", "expected", "inserted", "skipped", "failed", "loaded_sources", "errors"},
        {"schema_version", "run_id", "manifest_path", "manifest_sha256", "status", "expected", "inserted", "skipped", "failed", "loaded_sources", "errors"},
        {"schema_version": "str", "run_id": "str_or_none", "manifest_path": "str", "manifest_sha256": "sha256_or_none", "status": "str", "expected": "int", "inserted": "int", "skipped": "int", "failed": "int", "loaded_sources": "list", "errors": "list"},
    ),
    "provenance": _contract(
        "scripts.graph_provenance_reconcile",
        "graph_provenance_reconciliation_v1",
        {"schema_version", "generated_at", "records", "unique_source_ids", "source_ids", "doc_ids", "counts", "passed", "items", "state_snapshot", "errors"},
        {"schema_version", "generated_at", "records", "unique_source_ids", "source_ids", "doc_ids", "counts", "passed", "items", "errors", "state_snapshot"},
        {"schema_version": "str", "generated_at": "str", "records": "int", "unique_source_ids": "int", "source_ids": "list", "doc_ids": "list", "counts": "dict", "passed": "bool", "items": "list", "state_snapshot": "dict", "errors": "list"},
    ),
    "security": _contract(
        "scripts.security_release_scan",
        "security_release_scan_v2",
        {"schema_version", "status", "candidate_files", "inventory_sha256", "scanned_files", "skipped_files", "excluded_roots", "binary_files", "unreadable_files", "errors", "protected_files", "protected_occurrences", "metadata_occurrences", "tracked_protected_files", "staged_protected_files", "git_inspection_errors", "finding_files", "occurrences", "findings"},
        {"schema_version", "status", "candidate_files", "inventory_sha256", "scanned_files", "skipped_files", "excluded_roots", "binary_files", "unreadable_files", "errors", "protected_files", "protected_occurrences", "metadata_occurrences", "tracked_protected_files", "staged_protected_files", "git_inspection_errors", "finding_files", "occurrences", "findings"},
        {"schema_version": "str", "status": "str", "candidate_files": "int", "inventory_sha256": "sha256", "scanned_files": "int", "skipped_files": "list", "excluded_roots": "list", "binary_files": "int", "unreadable_files": "int", "errors": "list", "protected_files": "list", "protected_occurrences": "int", "metadata_occurrences": "int", "tracked_protected_files": "list", "staged_protected_files": "list", "git_inspection_errors": "list", "finding_files": "int", "occurrences": "int", "findings": "list"},
    ),
    "backup": _contract(
        "scripts.validate_backup_restore",
        "backup_restore_closeout_v1",
        {"schema_version", "generated_at", "manifest", "backup_manifest_sha256", "backup_inventory_sha256", "root", "files_declared", "files_checked", "nested_paths", "canonical_mapping", "errors", "status", "mode"},
        {"schema_version", "generated_at", "manifest", "backup_manifest_sha256", "backup_inventory_sha256", "root", "files_declared", "files_checked", "nested_paths", "canonical_mapping", "errors", "status", "mode"},
        {"schema_version": "str", "generated_at": "str", "manifest": "str", "backup_manifest_sha256": "sha256", "backup_inventory_sha256": "sha256", "root": "str", "files_declared": "int", "files_checked": "int", "nested_paths": "list", "canonical_mapping": "list", "errors": "list", "status": "str", "mode": "str"},
    ),
    "portable_paths": _contract(
        "scripts.migrate_portable_paths",
        "portable_path_migration_v1",
        {"schema_version", "project_root", "files_scanned", "scanned_files", "inventory_sha256", "changed_files", "field_changes", "unresolved", "status", "mode", "errors"},
        {"schema_version", "project_root", "files_scanned", "scanned_files", "inventory_sha256", "changed_files", "field_changes", "unresolved", "status", "mode", "errors"},
        {"schema_version": "str", "project_root": "str", "files_scanned": "int", "scanned_files": "int", "inventory_sha256": "sha256", "changed_files": "int", "field_changes": "int", "unresolved": "list", "status": "str", "mode": "str", "errors": "list"},
    ),
    "cold_graph": _contract(
        "scripts.cold_graph_check",
        "cold_graph_extraction_v1",
        {"schema_version", "generated_at", "status", "workspace", "candidate_path", "manifest_path", "manifest_sha256", "manifest_run_id", "config_identity_sha256", "effective_cold_runtime", "source_proof_before_load", "source_proof_sha256", "loaded_doc_proof", "storage_inventory", "source_ids", "doc_ids", "source_storage_copied", "workspace_inventory", "empty_before", "complete", "resume", "batch_size", "batch_timeout_seconds", "checkpoint_path", "accepted_manifest_load_report_path", "source_results", "cold_graph_attempt", "errors"},
        {"schema_version", "generated_at", "status", "workspace", "candidate_path", "manifest_path", "manifest_sha256", "manifest_run_id", "config_identity_sha256", "effective_cold_runtime", "source_proof_before_load", "source_proof_sha256", "loaded_doc_proof", "storage_inventory", "source_ids", "doc_ids", "source_storage_copied", "workspace_inventory", "empty_before", "complete", "resume", "batch_size", "batch_timeout_seconds", "checkpoint_path", "accepted_manifest_load_report_path", "source_results", "cold_graph_attempt", "errors", "elapsed_seconds", "reason", "exception_type", "exception_message", "release_identity_error"},
        {"schema_version": "str", "generated_at": "str", "status": "str", "workspace": "str", "candidate_path": "str", "manifest_path": "str", "manifest_sha256": "sha256_or_none", "manifest_run_id": "str_or_none", "config_identity_sha256": "sha256_or_none", "effective_cold_runtime": "dict_or_none", "source_proof_before_load": "dict", "source_proof_sha256": "sha256_or_none", "loaded_doc_proof": "dict", "storage_inventory": "dict", "source_ids": "list", "doc_ids": "list", "source_storage_copied": "bool_or_none", "workspace_inventory": "dict", "empty_before": "bool", "complete": "bool", "resume": "bool", "batch_size": "int", "batch_timeout_seconds": "number", "checkpoint_path": "str", "accepted_manifest_load_report_path": "str", "source_results": "list", "cold_graph_attempt": "dict", "errors": "list"},
    ),
    "late_fusion_smoke": _contract(
        "scripts.late_fusion_activation_smoke",
        "late_fusion_smoke_v1",
        {"schema_version", "late_fusion_enabled", "reranker_enabled", "wiki_enabled", "cases", "cases_sha256", "official_cases_sha256", "passed", "errors", "attempt_id", "attempts", "attempt_history_path"},
        {"schema_version", "late_fusion_enabled", "reranker_enabled", "wiki_enabled", "cases", "cases_sha256", "official_cases_sha256", "passed", "errors", "attempt_id", "attempts", "attempt_history_path"},
        {"schema_version": "str", "late_fusion_enabled": "bool", "reranker_enabled": "bool", "wiki_enabled": "bool", "cases": "list", "cases_sha256": "sha256", "official_cases_sha256": "sha256", "passed": "bool", "errors": "list", "attempt_id": "str", "attempts": "list", "attempt_history_path": "str_or_none"},
    ),
    "tests": _contract(
        "scripts.build_release_test_evidence",
        "release_test_gate_v1",
        {"schema_version", "status", "summary", "summary_path", "summary_sha256", "execution_mode", "runner_receipts_path", "runner_receipts_sha256", "runner_lifecycle_path", "runner_lifecycle_sha256", "checks", "errors"},
        {"schema_version", "status", "summary", "summary_path", "summary_sha256", "execution_mode", "runner_receipts_path", "runner_receipts_sha256", "runner_lifecycle_path", "runner_lifecycle_sha256", "checks", "errors", "base_ref", "base_commit", "merge_base", "head_commit", "diff_check_argv"},
        {"schema_version": "str", "status": "str", "summary": "dict", "summary_path": "str", "summary_sha256": "sha256", "execution_mode": "str", "runner_receipts_path": "str", "runner_receipts_sha256": "sha256", "runner_lifecycle_path": "str", "runner_lifecycle_sha256": "sha256", "checks": "list", "errors": "list", "base_ref": "str_or_none", "base_commit": "str_or_none", "merge_base": "str_or_none", "head_commit": "str_or_none", "diff_check_argv": "list"},
    ),
    "risk_register": _contract(
        "scripts.build_release_risk_register",
        "release_risk_register_v1",
        {"schema_version", "risks", "errors"},
        {"schema_version", "risks", "errors"},
        {"schema_version": "str", "risks": "list", "errors": "list"},
    ),
}
RELEASE_REPORT_KINDS = frozenset(REPORT_CONTRACTS)
RELEASE_RESERVED_KINDS = frozenset({"accepted_manifest", "run_index", "evidence_manifest"})
_FIELD_ENUMS = {
    "status": {"accepted", "passed", "failed", "ok", "changes_pending"},
    "mode": {"check", "apply", "dry_run", "restore", "scan", "verify"},
}


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def static_environment_package_version_errors(packages: Any) -> list[str]:
    """Validate installed versions against project and dev dependency specs."""
    if not isinstance(packages, dict):
        return ["environment_package_inventory_invalid"]
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.utils import canonicalize_name
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return ["environment_version_validator_unavailable"]

    try:
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return ["environment_dependency_spec_unreadable:pyproject.toml"]
    project_table = project.get("project")
    if not isinstance(project_table, dict):
        return ["environment_dependency_spec_invalid:pyproject.toml"]
    raw_requirements = list(project_table.get("dependencies") or [])
    optional = project_table.get("optional-dependencies")
    if isinstance(optional, dict):
        raw_requirements.extend(optional.get("dev") or [])

    declared: dict[str, list[Any]] = {}
    errors: list[str] = []
    for raw in raw_requirements:
        if not isinstance(raw, str):
            errors.append("environment_dependency_spec_invalid:pyproject.toml")
            continue
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            errors.append(f"environment_dependency_spec_invalid:{raw}")
            continue
        declared.setdefault(canonicalize_name(requirement.name), []).append(requirement)

    for package in STATIC_ENVIRONMENT_PACKAGE_NAMES:
        canonical = canonicalize_name(package)
        requirements = declared.get(canonical)
        if not requirements:
            errors.append(f"environment_dependency_spec_missing:{package}")
            continue
        raw_version = packages.get(package)
        if not isinstance(raw_version, str) or not raw_version.strip():
            continue
        try:
            version = Version(raw_version.strip())
        except InvalidVersion:
            errors.append(f"environment_package_version_invalid:{package}")
            continue
        if any(
            requirement.specifier and version not in requirement.specifier
            for requirement in requirements
        ):
            errors.append(f"environment_package_version_mismatch:{package}")
    return errors


def static_environment_receipt_errors(value: Any) -> list[str]:
    """Validate the machine-readable static-gate environment receipt."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["environment_not_object"]
    current = current_static_environment_snapshot()
    current_errors = current.get("errors")
    if isinstance(current_errors, list):
        errors.extend(str(item) for item in current_errors)
    else:
        errors.append("environment_current_snapshot_invalid")
    current_packages = current.get("packages")
    if not isinstance(current_packages, dict):
        current_packages = {}
        errors.append("environment_current_package_inventory_invalid")
    current_command_versions = current.get("command_versions")
    if not isinstance(current_command_versions, dict):
        current_command_versions = {}
        errors.append("environment_current_command_versions_invalid")
    if value.get("status") != "passed":
        errors.append("environment_status_not_passed")
    commands = value.get("commands")
    if not isinstance(commands, dict):
        errors.append("environment_commands_invalid")
    else:
        if set(commands) != set(STATIC_ENVIRONMENT_COMMAND_NAMES):
            errors.append("environment_command_set_mismatch")
        for name in STATIC_ENVIRONMENT_COMMAND_NAMES:
            receipt = commands.get(name)
            if not isinstance(receipt, dict):
                errors.append(f"environment_command_missing:{name}")
                continue
            if set(receipt) != {"argv", "status", "exit_code", "version", "error"}:
                errors.append(f"environment_command_fields_invalid:{name}")
            if receipt.get("status") != "passed" or receipt.get("exit_code") != 0:
                errors.append(f"environment_command_failed:{name}")
            version = receipt.get("version")
            if not isinstance(version, str) or not version.strip():
                errors.append(f"environment_command_version_missing:{name}")
            elif version != current_command_versions.get(name):
                errors.append(f"environment_command_version_mismatch:{name}")
            if receipt.get("error") is not None:
                errors.append(f"environment_command_error_not_empty:{name}")
            argv = receipt.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
                errors.append(f"environment_command_argv_invalid:{name}")
            elif not _environment_argv_matches(argv, _expected_environment_command_argv(name)):
                errors.append(f"environment_command_argv_mismatch:{name}")
    pip_show = value.get("pip_show")
    if not isinstance(pip_show, dict) or pip_show.get("status") != "passed":
        errors.append("environment_pip_show_failed")
    else:
        if set(pip_show) != {
            "commands",
            "status",
            "packages",
            "missing_packages",
            "version_errors",
            "error",
        }:
            errors.append("environment_pip_show_fields_invalid")
        packages = pip_show.get("packages")
        if not isinstance(packages, dict):
            errors.append("environment_package_inventory_invalid")
            package_versions: dict[str, Any] = {}
        else:
            package_versions = packages
            if set(packages) != set(STATIC_ENVIRONMENT_PACKAGE_NAMES):
                errors.append("environment_package_set_mismatch")
            for name in STATIC_ENVIRONMENT_PACKAGE_NAMES:
                version = packages.get(name)
                if not isinstance(version, str) or not version.strip():
                    errors.append(f"environment_package_missing:{name}")
                elif version != current_packages.get(name):
                    errors.append(f"environment_installed_version_mismatch:{name}")
            errors.extend(static_environment_package_version_errors(packages))
        if pip_show.get("missing_packages") != []:
            errors.append("environment_missing_packages_not_empty")
        if pip_show.get("version_errors") != []:
            errors.append("environment_version_errors_not_empty")
        if pip_show.get("error") is not None:
            errors.append("environment_pip_show_error_not_empty")
        pip_commands = pip_show.get("commands")
        if not isinstance(pip_commands, list) or len(pip_commands) != len(STATIC_ENVIRONMENT_PACKAGE_NAMES):
            errors.append("environment_pip_show_commands_invalid")
        else:
            seen_packages: set[str] = set()
            for receipt in pip_commands:
                if not isinstance(receipt, dict):
                    errors.append("environment_pip_show_receipt_invalid")
                    continue
                if set(receipt) != {
                    "package",
                    "argv",
                    "status",
                    "exit_code",
                    "observed_name",
                    "observed_version",
                    "error",
                }:
                    errors.append("environment_pip_show_receipt_fields_invalid")
                package = receipt.get("package")
                if isinstance(package, str):
                    if package in seen_packages:
                        errors.append(f"environment_pip_show_duplicate_package:{package}")
                    seen_packages.add(package)
                if package not in STATIC_ENVIRONMENT_PACKAGE_NAMES or receipt.get("status") != "passed" or receipt.get("exit_code") != 0:
                    errors.append(f"environment_pip_show_package_failed:{package}")
                argv = receipt.get("argv")
                if not isinstance(argv, list) or any(not isinstance(item, str) or not item for item in argv):
                    errors.append(f"environment_pip_show_argv_invalid:{package}")
                elif package in STATIC_ENVIRONMENT_PACKAGE_NAMES and not _environment_argv_matches(
                    argv,
                    [sys.executable, "-m", "pip", "show", package],
                ):
                    errors.append(f"environment_pip_show_argv_mismatch:{package}")
                observed_name = receipt.get("observed_name")
                if not isinstance(observed_name, str) or not observed_name.strip():
                    errors.append(f"environment_pip_show_observed_name_missing:{package}")
                elif package in STATIC_ENVIRONMENT_PACKAGE_NAMES and observed_name != normalise_distribution_name(package):
                    errors.append(f"environment_pip_show_observed_name_mismatch:{package}")
                observed_version = receipt.get("observed_version")
                if not isinstance(observed_version, str) or not observed_version.strip():
                    errors.append(f"environment_pip_show_observed_version_missing:{package}")
                elif package in STATIC_ENVIRONMENT_PACKAGE_NAMES and observed_version != package_versions.get(package):
                    errors.append(f"environment_pip_show_observed_version_mismatch:{package}")
                if package in STATIC_ENVIRONMENT_PACKAGE_NAMES and observed_version != current_packages.get(package):
                    errors.append(f"environment_pip_show_installed_version_mismatch:{package}")
                if receipt.get("error") is not None:
                    errors.append(f"environment_pip_show_error_not_empty:{package}")
            if seen_packages != set(STATIC_ENVIRONMENT_PACKAGE_NAMES):
                errors.append("environment_pip_show_package_set_mismatch")
    dependency_files = value.get("dependency_files")
    expected_dependency_receipt = dependency_spec_receipt()
    if not isinstance(dependency_files, list) or len(dependency_files) != 2:
        errors.append("environment_dependency_files_invalid")
    else:
        paths: set[str] = set()
        for item in dependency_files:
            if not isinstance(item, dict):
                errors.append("environment_dependency_file_invalid")
                continue
            path = item.get("path")
            paths.add(path)
            if path not in {"pyproject.toml", "requirements.txt"}:
                errors.append(f"environment_dependency_path_invalid:{path}")
            if not isinstance(item.get("bytes"), int) or isinstance(item.get("bytes"), bool) or item["bytes"] < 0:
                errors.append(f"environment_dependency_bytes_invalid:{path}")
            if not _is_sha256(item.get("sha256")):
                errors.append(f"environment_dependency_hash_invalid:{path}")
        if paths != {"pyproject.toml", "requirements.txt"}:
            errors.append("environment_dependency_file_set_mismatch")
        if dependency_files != expected_dependency_receipt["files"]:
            errors.append("environment_dependency_files_mismatch")
    if not _is_sha256(value.get("dependency_spec_identity_sha256")):
        errors.append("environment_dependency_identity_invalid")
    elif value.get("dependency_spec_identity_sha256") != expected_dependency_receipt["identity_sha256"]:
        errors.append("environment_dependency_identity_mismatch")
    errors.extend(expected_dependency_receipt["errors"])
    if value.get("errors") != []:
        errors.append("environment_receipt_contains_errors")
    return sorted(set(errors))


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def strict_json_loads(value: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate object keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=reject_duplicates)


def _release_secret_texts(value: str | bytes) -> tuple[_ReleaseSecretText, ...]:
    if isinstance(value, str):
        return (_ReleaseSecretText(value, "unicode"),)
    data = bytes(value)
    representations = [_ReleaseSecretText(data.decode("latin-1"), "binary-latin-1")]
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            representations.append(
                _ReleaseSecretText(data[3:].decode("utf-8"), "utf-8-sig", 3)
            )
        except UnicodeDecodeError:
            pass
    else:
        try:
            representations.append(_ReleaseSecretText(data.decode("utf-8"), "utf-8"))
        except UnicodeDecodeError:
            pass

    if len(data) % 2 == 0:
        for encoding, bom, byte_offset in (
            ("utf-16-le", b"\xff\xfe", 2),
            ("utf-16-be", b"\xfe\xff", 2),
        ):
            payload = data[byte_offset:] if data.startswith(bom) else data
            offset = byte_offset if data.startswith(bom) else 0
            try:
                representations.append(
                    _ReleaseSecretText(payload.decode(encoding), encoding, offset)
                )
            except UnicodeDecodeError:
                continue
    return tuple(representations)


def release_secret_matches(value: str | bytes) -> tuple[ReleaseSecretMatch, ...]:
    """Return safe spans for every matched category without returning values."""
    matches: list[ReleaseSecretMatch] = []
    for representation in _release_secret_texts(value):
        for name, pattern in _RELEASE_SECRET_PATTERNS:
            for match in pattern.finditer(representation.text):
                start, end = representation.physical_span(match.start(), match.end())
                matches.append(
                    ReleaseSecretMatch(name, start, end, representation.encoding)
                )
    return tuple(
        sorted(matches, key=lambda item: (item.start, item.end, item.encoding, item.category))
    )


def release_secret_groups(
    matches: tuple[ReleaseSecretMatch, ...] | list[ReleaseSecretMatch],
) -> tuple[ReleaseSecretFinding, ...]:
    """Merge overlapping category matches into physical secret occurrences."""
    groups: list[ReleaseSecretFinding] = []
    for match in sorted(matches, key=lambda item: (item.start, item.end, item.encoding, item.category)):
        if groups and match.start < groups[-1].end:
            previous = groups[-1]
            groups[-1] = ReleaseSecretFinding(
                start=min(previous.start, match.start),
                end=max(previous.end, match.end),
                categories=tuple(sorted(set(previous.categories) | {match.category})),
                encodings=tuple(sorted(set(previous.encodings) | {match.encoding})),
            )
        else:
            groups.append(
                ReleaseSecretFinding(
                    start=match.start,
                    end=match.end,
                    categories=(match.category,),
                    encodings=(match.encoding,),
                )
            )
    return tuple(groups)


def release_secret_findings(value: str | bytes) -> dict[str, int]:
    """Count categories across unique physical findings without exposing values."""
    counts: dict[str, int] = {}
    for finding in release_secret_groups(list(release_secret_matches(value))):
        for category in finding.categories:
            counts[category] = counts.get(category, 0) + 1
    return counts


def release_secret_count(value: str | bytes) -> int:
    return len(release_secret_groups(list(release_secret_matches(value))))


def assert_no_release_secrets(value: str | bytes, *, label: str) -> None:
    findings = release_secret_findings(value)
    if findings:
        raise ValueError(
            f"release_secret_material_detected:{label}:"
            f"types={','.join(sorted(findings))}:count={release_secret_count(value)}"
        )


def safe_resolve_release_path(
    path: Path,
    *,
    label: str,
    strict: bool = False,
) -> Path:
    """Resolve an untrusted release path without exposing raw failure details."""
    assert_no_release_secrets(str(path), label=f"{label}_lexical")
    resolution_error: ReleaseEvidenceError | None = None
    try:
        resolved = path.resolve(strict=strict)
    except (OSError, RuntimeError, ValueError) as exc:
        diagnostic = safe_exception_diagnostic(
            "release_path_resolution_failed",
            exc,
            component=label,
        )
        resolution_error = ReleaseEvidenceError(
            ":".join(
                (
                    diagnostic["error_code"],
                    diagnostic["component"],
                    diagnostic["exception_type"],
                    diagnostic["message_sha256"],
                )
            )
        )
        resolved = None
    if resolution_error is not None:
        raise resolution_error from None
    if resolved is None:  # pragma: no cover - defensive type narrowing
        raise ReleaseEvidenceError("release_path_resolution_failed")
    assert_no_release_secrets(str(resolved), label=label)
    return resolved


def redact_release_secrets(value: str) -> tuple[str, int]:
    """Redact secret-shaped substrings while revealing neither match nor value."""
    redacted = str(value)
    findings = release_secret_groups(list(release_secret_matches(redacted)))
    for finding in reversed(findings):
        redacted = redacted[: finding.start] + "[REDACTED]" + redacted[finding.end :]
    return redacted, len(findings)


def sanitize_release_value(value: Any) -> tuple[Any, int]:
    """Recursively redact secret material before any release artifact is written."""
    if isinstance(value, str):
        return redact_release_secrets(value)
    if isinstance(value, list):
        result: list[Any] = []
        count = 0
        for item in value:
            sanitized, replacements = sanitize_release_value(item)
            result.append(sanitized)
            count += replacements
        return result, count
    if isinstance(value, tuple):
        sanitized, count = sanitize_release_value(list(value))
        return tuple(sanitized), count
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            sanitized_key, key_replacements = (
                redact_release_secrets(key) if isinstance(key, str) else (key, 0)
            )
            sanitized, replacements = sanitize_release_value(item)
            if sanitized_key in result:
                raise ValueError("release sanitization produced a duplicate object key")
            result[sanitized_key] = sanitized
            count += key_replacements + replacements
        return result, count
    return value, 0


_DIAGNOSTIC_FIELD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EXCEPTION_FINGERPRINT_MAX_BYTES = 4096
_EXCEPTION_FINGERPRINT_MAX_DEPTH = 4
_EXCEPTION_FINGERPRINT_MAX_ITEMS = 32
_EXCEPTION_FINGERPRINT_MAX_SCALAR_BYTES = 1024


def _append_fingerprint_bytes(buffer: bytearray, value: bytes) -> None:
    remaining = _EXCEPTION_FINGERPRINT_MAX_BYTES - len(buffer)
    if remaining > 0:
        buffer.extend(value[:remaining])


def _append_fingerprint_block(buffer: bytearray, value: bytes) -> None:
    """Append one length-delimited structural block within the global byte cap."""
    _append_fingerprint_bytes(buffer, len(value).to_bytes(4, "big", signed=False))
    _append_fingerprint_bytes(buffer, value)


def _exception_fingerprint_block(
    value: Any,
    *,
    depth: int,
    seen: set[int],
) -> bytes:
    """Encode a sortable bounded block without sharing mutable traversal state."""
    block = bytearray()
    _encode_exception_fingerprint_value(
        value,
        block,
        depth=depth,
        seen=set(seen),
    )
    return bytes(block)


def _opaque_type_fingerprint(value_type: type[Any]) -> bytes:
    """Return a bounded module-qualified type tag without rendering user values."""
    material = bytearray(b"opaque_type_v2")
    for attribute in ("__module__", "__qualname__"):
        try:
            value = type.__getattribute__(value_type, attribute)
        except Exception:
            value = None
        if type(value) is not str:
            encoded = b"unknown"
            original_length = 0
        else:
            encoded = value[:128].encode("utf-8", errors="surrogatepass")
            original_length = len(value)
        material.extend(original_length.to_bytes(8, "big", signed=False))
        material.extend(len(encoded).to_bytes(2, "big", signed=False))
        material.extend(encoded)
    return hashlib.sha256(material).digest()[:16]


def _encode_exception_fingerprint_value(
    value: Any,
    buffer: bytearray,
    *,
    depth: int,
    seen: set[int],
) -> None:
    """Encode exact built-ins without invoking user rendering or iteration hooks."""
    if len(buffer) >= _EXCEPTION_FINGERPRINT_MAX_BYTES:
        return
    value_type = type(value)
    if value is None:
        _append_fingerprint_bytes(buffer, b"n;")
        return
    if value_type is bool:
        _append_fingerprint_bytes(buffer, b"b1;" if value else b"b0;")
        return
    if value_type is int:
        bit_length = value.bit_length()
        _append_fingerprint_bytes(
            buffer,
            b"i" + bit_length.to_bytes(8, "big", signed=False) + (b"-" if value < 0 else b"+"),
        )
        if bit_length <= _EXCEPTION_FINGERPRINT_MAX_SCALAR_BYTES * 8:
            magnitude = abs(value)
            byte_count = max(1, (bit_length + 7) // 8)
            _append_fingerprint_bytes(buffer, magnitude.to_bytes(byte_count, "big"))
        _append_fingerprint_bytes(buffer, b";")
        return
    if value_type is float:
        _append_fingerprint_bytes(buffer, b"f" + struct.pack(">d", value) + b";")
        return
    if value_type is str:
        encoded = value[:_EXCEPTION_FINGERPRINT_MAX_SCALAR_BYTES].encode(
            "utf-8", errors="surrogatepass"
        )
        _append_fingerprint_bytes(
            buffer,
            b"s" + len(value).to_bytes(8, "big", signed=False) + encoded + b";",
        )
        return
    if value_type is bytes:
        _append_fingerprint_bytes(
            buffer,
            b"y"
            + len(value).to_bytes(8, "big", signed=False)
            + value[:_EXCEPTION_FINGERPRINT_MAX_SCALAR_BYTES]
            + b";",
        )
        return
    if depth >= _EXCEPTION_FINGERPRINT_MAX_DEPTH:
        _append_fingerprint_bytes(buffer, b"depth;")
        return
    if value_type not in (tuple, list, dict, set, frozenset):
        _append_fingerprint_bytes(
            buffer, b"opaque" + _opaque_type_fingerprint(value_type) + b";"
        )
        return

    identity = id(value)
    if identity in seen:
        _append_fingerprint_bytes(buffer, b"cycle;")
        return
    seen.add(identity)
    try:
        if value_type in (tuple, list):
            _append_fingerprint_bytes(
                buffer,
                (b"tuple" if value_type is tuple else b"list")
                + len(value).to_bytes(8, "big", signed=False)
                + b"[",
            )
            for index in range(min(len(value), _EXCEPTION_FINGERPRINT_MAX_ITEMS)):
                item = (
                    tuple.__getitem__(value, index)
                    if value_type is tuple
                    else list.__getitem__(value, index)
                )
                _encode_exception_fingerprint_value(
                    item, buffer, depth=depth + 1, seen=seen
                )
            _append_fingerprint_bytes(buffer, b"];")
        elif value_type is dict:
            _append_fingerprint_bytes(
                buffer, b"dict" + len(value).to_bytes(8, "big", signed=False) + b"["
            )
            if len(value) > _EXCEPTION_FINGERPRINT_MAX_ITEMS:
                _append_fingerprint_bytes(buffer, b"unordered_truncated;")
            else:
                pair_blocks: list[bytes] = []
                for key, item in dict.items(value):
                    pair = bytearray()
                    _append_fingerprint_block(
                        pair,
                        _exception_fingerprint_block(
                            key,
                            depth=depth + 1,
                            seen=seen,
                        ),
                    )
                    _append_fingerprint_block(
                        pair,
                        _exception_fingerprint_block(
                            item,
                            depth=depth + 1,
                            seen=seen,
                        ),
                    )
                    pair_blocks.append(bytes(pair))
                for pair in sorted(pair_blocks):
                    _append_fingerprint_block(buffer, pair)
            _append_fingerprint_bytes(buffer, b"];")
        else:
            _append_fingerprint_bytes(
                buffer,
                (b"set" if value_type is set else b"frozenset")
                + len(value).to_bytes(8, "big", signed=False)
                + b"[",
            )
            if len(value) > _EXCEPTION_FINGERPRINT_MAX_ITEMS:
                _append_fingerprint_bytes(buffer, b"unordered_truncated;")
            else:
                iterator = (
                    set.__iter__(value)
                    if value_type is set
                    else frozenset.__iter__(value)
                )
                blocks = sorted(
                    _exception_fingerprint_block(
                        item,
                        depth=depth + 1,
                        seen=seen,
                    )
                    for item in iterator
                )
                for block in blocks:
                    _append_fingerprint_block(buffer, block)
            _append_fingerprint_bytes(buffer, b"];")
    finally:
        seen.remove(identity)


def _safe_exception_message_sha256(exc: BaseException) -> str:
    """Hash bounded exception structure without invoking untrusted hooks."""
    material = bytearray(b"safe_exception_args_v1;")
    try:
        args = BaseException.args.__get__(exc, BaseException)
    except Exception:
        args = ()
        _append_fingerprint_bytes(material, b"args_unavailable;")
    try:
        _encode_exception_fingerprint_value(args, material, depth=0, seen=set())
    except Exception:
        material = bytearray(b"safe_exception_args_v1;encoding_failed;")
    return hashlib.sha256(bytes(material)).hexdigest()


def _safe_diagnostic_field(value: Any, *, fallback: str) -> str:
    if type(value) is not str:
        return fallback
    if len(value) > 128:
        return fallback
    if not _DIAGNOSTIC_FIELD_RE.fullmatch(value):
        return fallback
    if release_secret_count(value):
        return fallback
    return value


def _safe_exception_type(exc: BaseException) -> str:
    try:
        name = type.__getattribute__(type(exc), "__name__")
    except Exception:
        name = None
    return _safe_diagnostic_field(name, fallback="BaseException")


def safe_exception_diagnostic(code: str, exc: BaseException, *, component: str) -> dict[str, str]:
    """Return a bounded diagnostic without invoking exception rendering hooks."""
    diagnostic = {
        "error_code": _safe_diagnostic_field(code, fallback="release_error"),
        "component": _safe_diagnostic_field(component, fallback="release_component"),
        "exception_type": _safe_exception_type(exc),
        "message_sha256": _safe_exception_message_sha256(exc),
    }
    serialized = json.dumps(
        diagnostic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        assert_no_release_secrets(serialized, label="exception_diagnostic")
    except Exception:
        diagnostic = {
            "error_code": "release_error",
            "component": "release_component",
            "exception_type": "BaseException",
            "message_sha256": hashlib.sha256(b"safe_exception_fallback_v1").hexdigest(),
        }
    return diagnostic


def safe_exception_token(code: str, exc: BaseException, *, component: str) -> str:
    """Return one compact, scanner-safe token for string-valued error fields."""
    diagnostic = safe_exception_diagnostic(code, exc, component=component)
    return ":".join(
        (
            diagnostic["error_code"],
            diagnostic["component"],
            diagnostic["exception_type"],
            diagnostic["message_sha256"],
        )
    )


def safe_release_exception(
    code: str,
    exc: BaseException,
    *,
    component: str,
) -> ReleaseEvidenceError:
    """Build a public exception that contains no raw lower-level message."""
    diagnostic = safe_exception_diagnostic(code, exc, component=component)
    return ReleaseEvidenceError(
        ":".join(
            (
                diagnostic["error_code"],
                diagnostic["component"],
                diagnostic["exception_type"],
                diagnostic["message_sha256"],
            )
        )
    )


def best_effort_with_pending_interrupt(
    pending_interrupt: BaseException | None,
    finalizer: Any,
) -> Any:
    """Run finalization without allowing a secondary failure to replace an interrupt."""
    try:
        return finalizer()
    except BaseException:
        if pending_interrupt is None:
            raise
        return None


def run_release_cli(main_func: Any, *, component: str) -> int:
    """Run a release producer without publishing an untrusted traceback."""
    stderr_buffer = io.StringIO()
    diagnostic: dict[str, Any] | None = None
    result = 0
    with contextlib.redirect_stderr(stderr_buffer):
        try:
            result = main_func()
        except ReleaseArgumentParserError as exc:
            diagnostic = safe_exception_diagnostic(
                "release_cli_argument_rejected",
                exc,
                component=component,
            )
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
            if exit_code != 0:
                diagnostic = safe_exception_diagnostic(
                    "release_cli_exited",
                    exc,
                    component=component,
                )
            result = exit_code
        except Exception as exc:
            diagnostic = {
                **publication_outcome(exc),
                **safe_exception_diagnostic(
                    "release_cli_failed",
                    exc,
                    component=component,
                ),
            }

    buffered_stderr = stderr_buffer.getvalue()
    if diagnostic is None and buffered_stderr:
        try:
            assert_no_release_secrets(buffered_stderr, label=f"{component}_stderr")
        except Exception as exc:
            diagnostic = safe_exception_diagnostic(
                "release_cli_stderr_rejected",
                exc,
                component=component,
            )
        else:
            sys.stderr.write(buffered_stderr)

    if diagnostic is not None:
        print(
            json.dumps(
                {"status": "failed", **diagnostic},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    return int(result or 0)


def _stable_stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _read_stable_file_bytes(path: Path) -> bytes:
    """Read one file handle and reject path/handle identity changes around the read."""
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = path.stat(follow_symlinks=False)
    if not (
        _stable_stat_identity(before)
        == _stable_stat_identity(opened)
        == _stable_stat_identity(after)
    ):
        raise RuntimeError("artifact file changed during read")
    return data


def _artifact_tree_snapshot(root: Path) -> list[dict[str, Any]]:
    """Capture one analyzed namespace/content snapshot without split reads."""
    descriptor_alias = _is_open_directory_descriptor_alias(root)
    root_before = root.stat(follow_symlinks=descriptor_alias)
    if not stat.S_ISDIR(root_before.st_mode) or (not descriptor_alias and _path_is_link_or_reparse(root)):
        raise RuntimeError("artifact tree root boundary violation")
    records: list[dict[str, Any]] = [
        {
            "path": ".",
            "identity": list(_stable_stat_identity(root_before)),
            "link": False,
            "node_type": "directory",
            "data": None,
        }
    ]
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        before = path.lstat()
        is_link = _path_is_link_or_reparse(path)
        node_type = (
            "link"
            if is_link
            else "file"
            if stat.S_ISREG(before.st_mode)
            else "directory"
            if stat.S_ISDIR(before.st_mode)
            else "other"
        )
        data: bytes | None = None
        if node_type == "file":
            data = _read_stable_file_bytes(path)
            after = path.stat(follow_symlinks=False)
            if _stable_stat_identity(before) != _stable_stat_identity(after):
                raise RuntimeError("artifact file changed during snapshot")
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "identity": list(_stable_stat_identity(before)),
                "link": is_link,
                "node_type": node_type,
                "data": data,
            }
        )
    root_after = root.stat(follow_symlinks=descriptor_alias)
    if _stable_stat_identity(root_before) != _stable_stat_identity(root_after):
        raise RuntimeError("artifact tree root changed during snapshot")
    return records


def _artifact_tree_snapshot_sha256(records: list[dict[str, Any]]) -> str:
    inventory = [
        {
            "path": record["path"],
            "identity": record["identity"],
            "link": record["link"],
            "node_type": record["node_type"],
            **(
                {"sha256": hashlib.sha256(record["data"]).hexdigest()}
                if isinstance(record.get("data"), bytes)
                else {}
            ),
        }
        for record in records
    ]
    return hashlib.sha256(canonical_json(inventory)).hexdigest()


def capture_release_artifact_tree_snapshot(root: Path) -> list[dict[str, Any]]:
    """Capture the bytes later used by preflight, scanning, sealing, and commit."""
    return _artifact_tree_snapshot(root.absolute())


def materialize_release_artifact_tree_snapshot(
    records: list[dict[str, Any]], destination: Path
) -> None:
    """Materialize a private validation copy from one already captured snapshot."""
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise RuntimeError("artifact snapshot destination must be an empty directory")
    else:
        destination.mkdir(mode=0o700)
    directories = sorted(
        (
            record
            for record in records
            if record["path"] != "." and record["node_type"] == "directory"
        ),
        key=lambda record: (len(Path(str(record["path"])).parts), str(record["path"])),
    )
    for record in directories:
        relative = Path(str(record["path"]))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RuntimeError("artifact snapshot path invalid")
        (destination / relative).mkdir()
    for record in records:
        relative = str(record["path"])
        if relative == "." or record["node_type"] == "directory":
            continue
        if record["node_type"] != "file" or bool(record["link"]):
            raise RuntimeError("artifact snapshot contains unsupported entry")
        data = record.get("data")
        if not isinstance(data, bytes):
            raise RuntimeError("artifact snapshot file bytes missing")
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise RuntimeError("artifact snapshot path invalid")
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


def _artifact_tree_inventory_sha256(root: Path) -> str:
    return _artifact_tree_snapshot_sha256(_artifact_tree_snapshot(root))


def scan_release_artifact_tree(
    root: Path, *, analyzed_snapshot: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Scan the exact physical artifact bytes beneath ``root`` fail-closed."""
    root = root.absolute()
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned_files = 0
    total_metadata_occurrences = 0
    try:
        root_invalid = not _is_safe_directory_root(root)
    except OSError:
        root_invalid = True
    if root_invalid:
        return {
            "status": "failed",
            "scanned_files": 0,
            "finding_files": 0,
            "occurrences": 0,
            "metadata_occurrences": 0,
            "findings": [],
            "errors": [{"path": ".", "error": "artifact_root_missing"}],
        }
    try:
        if analyzed_snapshot is None:
            analyzed_snapshot = _artifact_tree_snapshot(root)
        analyzed_inventory_sha256 = _artifact_tree_snapshot_sha256(analyzed_snapshot)
    except (OSError, RuntimeError) as exc:
        diagnostic = safe_exception_diagnostic(
            "artifact_inventory_failed",
            exc,
            component="release_artifact_scan",
        )
        return {
            "status": "failed",
            "scanned_files": 0,
            "finding_files": 0,
            "occurrences": 0,
            "metadata_occurrences": 0,
            "inventory_sha256": None,
            "findings": [],
            "errors": [{"path": "[INVENTORY]", "error": diagnostic["error_code"]}],
        }
    for record in analyzed_snapshot:
        relative = str(record["path"])
        if relative == ".":
            continue
        safe_relative, metadata_occurrences = redact_release_secrets(relative)
        total_metadata_occurrences += metadata_occurrences
        if bool(record["link"]):
            errors.append({"path": safe_relative, "error": "artifact_path_boundary_violation"})
            continue
        node_type = str(record["node_type"])
        if node_type != "file":
            if metadata_occurrences:
                findings.append(
                    {
                        "path": safe_relative,
                        "bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                        "types": sorted(release_secret_findings(relative)),
                        "encodings": ["path-unicode"],
                        "occurrences": metadata_occurrences,
                        "metadata_occurrences": metadata_occurrences,
                        "node_type": node_type,
                    }
                )
            continue
        data = record.get("data")
        if not isinstance(data, bytes):
            errors.append(
                {
                    "path": safe_relative,
                    "error": "artifact_snapshot_missing_bytes",
                }
            )
            continue
        scanned_files += 1
        matches = release_secret_matches(data)
        secret_groups = release_secret_groups(list(matches))
        if secret_groups or metadata_occurrences:
            categories = sorted(
                {category for item in secret_groups for category in item.categories}
                | set(release_secret_findings(relative))
            )
            encodings = sorted(
                {encoding for item in secret_groups for encoding in item.encodings}
                | ({"path-unicode"} if metadata_occurrences else set())
            )
            findings.append(
                {
                    "path": safe_relative,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "types": categories,
                    "encodings": encodings,
                    "occurrences": len(secret_groups) + metadata_occurrences,
                    "metadata_occurrences": metadata_occurrences,
                }
            )
    try:
        final_inventory_sha256 = _artifact_tree_snapshot_sha256(
            _artifact_tree_snapshot(root)
        )
        if final_inventory_sha256 != analyzed_inventory_sha256:
            errors.append(
                {"path": "[FRESHNESS]", "error": "artifact_inventory_changed"}
            )
    except (OSError, RuntimeError):
        errors.append({"path": "[FRESHNESS]", "error": "artifact_freshness_failed"})
    occurrences = sum(int(item["occurrences"]) for item in findings)
    return {
        "status": (
            "passed"
            if not findings and not errors and total_metadata_occurrences == 0
            else "failed"
        ),
        "scanned_files": scanned_files,
        "finding_files": len(findings),
        "occurrences": occurrences,
        "metadata_occurrences": total_metadata_occurrences,
        "inventory_sha256": analyzed_inventory_sha256,
        "findings": findings,
        "errors": errors,
    }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _windows_durable_replace(source: Path, target: Path) -> None:
    """Atomically replace a Windows path and wait for move metadata to flush."""
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    flags = _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
    if not move_file_ex(str(source), str(target), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_rename_open_directory(handle: int, target: Path) -> None:
    """Rename the exact opened directory object, never a re-resolved source name."""
    import ctypes
    from ctypes import wintypes

    class FILE_RENAME_INFO(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        )

    encoded_name = str(target).encode("utf-16-le")
    name_offset = FILE_RENAME_INFO.FileName.offset
    # FILE_RENAME_INFO carries an explicit byte length, but a trailing UTF-16
    # NUL keeps older Windows implementations from reading adjacent memory.
    buffer_size = name_offset + len(encoded_name) + 2
    buffer = ctypes.create_string_buffer(buffer_size)
    info = ctypes.cast(buffer, ctypes.POINTER(FILE_RENAME_INFO)).contents
    info.ReplaceIfExists = 0
    info.RootDirectory = None
    info.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    set_information = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    if not set_information(handle, 3, buffer, buffer_size):  # FileRenameInfo
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_open_directory_for_publication(source: Path) -> int:
    """Open one directory for a later exact-object rename without delete sharing."""
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(source),
        0x40000000 | 0x00010000 | 0x80,
        0x1 | 0x2,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    if not close(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_flush_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    flush = ctypes.WinDLL("kernel32", use_last_error=True).FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    if not flush(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _posix_rename_noreplace(parent_fd: int, source_name: str, target_name: str) -> None:
    """Rename sibling directories without ever replacing an existing target."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace directory rename unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(target_name),
        1,  # RENAME_NOREPLACE
    )
    if result != 0:
        errno_value = ctypes.get_errno()
        raise OSError(errno_value, os.strerror(errno_value))


def _windows_durable_directory_replace(source: Path, target: Path) -> None:
    """Pin and rename the exact source directory, then flush its metadata."""
    handle = _windows_open_directory_for_publication(source)
    committed = False
    try:
        if _path_is_link_or_reparse(source) or not source.is_dir():
            raise RuntimeError("release publication source boundary violation")
        _windows_rename_open_directory(int(handle), target)
        committed = True
        _windows_flush_handle(handle)
    except Exception:
        if committed:
            raise ReleasePublicationCommittedError(
                "release_publication_committed_durability_uncertain"
            ) from None
        raise
    finally:
        _windows_close_handle(handle)


def durable_replace_in_directory(
    parent: PinnedDirectory, source_name: str, target_name: str
) -> None:
    """Replace sibling children under one already-pinned directory."""
    source = parent.child(source_name)
    target = parent.child(target_name)
    committed = False
    try:
        if os.name == "nt":
            if source.is_dir():
                _windows_durable_directory_replace(source, target)
            else:
                _windows_durable_replace(source, target)
            committed = True
        else:
            assert parent.directory_fd is not None
            os.replace(
                source_name,
                target_name,
                src_dir_fd=parent.directory_fd,
                dst_dir_fd=parent.directory_fd,
            )
            committed = True
            os.fsync(parent.directory_fd)
    except ReleasePublicationCommittedError:
        raise
    except Exception:
        if committed:
            raise ReleasePublicationCommittedError(
                "release_publication_committed_durability_uncertain"
            ) from None
        raise


def durable_unlink_in_directory(
    parent: PinnedDirectory, name: str, *, missing_ok: bool = False
) -> None:
    """Durably remove one logical child name under a pinned directory."""
    child = parent.child(name)
    if not child.exists():
        if missing_ok:
            return
        raise FileNotFoundError(child)
    committed = False
    try:
        if os.name == "nt":
            tombstone_name = (
                f".{name}.{os.getpid()}.{os.urandom(8).hex()}.deleted"
            )
            tombstone = parent.child(tombstone_name)
            _windows_durable_replace(child, tombstone)
            committed = True
            tombstone.unlink()
        else:
            assert parent.directory_fd is not None
            os.unlink(name, dir_fd=parent.directory_fd)
            committed = True
            os.fsync(parent.directory_fd)
    except ReleasePublicationCommittedError:
        raise
    except Exception:
        if committed:
            raise ReleasePublicationCommittedError(
                "release_unlink_committed_durability_uncertain"
            ) from None
        raise


def durable_replace(source: Path, target: Path) -> None:
    """Replace sibling paths while their parent identity is pinned."""
    source = source.absolute()
    target = target.absolute()
    if source.parent != target.parent:
        raise ValueError("durable replacement requires sibling paths")
    if source.is_dir():
        with prepared_publication_tree(source, target) as publication:
            publication.seal()
            publication.commit()
        return
    with pinned_directory(target.parent) as parent:
        durable_replace_in_directory(parent, source.name, target.name)


def publication_outcome(exc: BaseException) -> dict[str, Any]:
    """Return a small public state that never confuses committed with failure."""
    if isinstance(exc, ReleasePublicationCommittedError):
        outcome = {"status": "committed_durability_uncertain", "committed": True}
        details = getattr(exc, "publication_details", None)
        if isinstance(details, dict):
            recovery_action = details.get("recovery_action")
            if recovery_action in {
                "committed_publication_quarantined",
                "committed_publication_recovery_required",
                "redacted_family_finalized",
                "original_family_restored",
                "no_transition_found",
            }:
                outcome["recovery_action"] = recovery_action
            quarantine_path = details.get("quarantine_path")
            if (
                recovery_action == "committed_publication_quarantined"
                and isinstance(quarantine_path, str)
            ):
                outcome["quarantine_path"] = quarantine_path
        return outcome
    return {"status": "failed", "committed": False}


def atomic_write_bytes_in_directory(parent: PinnedDirectory, name: str, data: bytes) -> None:
    """Atomically write one child while its parent identity remains pinned."""
    temporary_name = f".{name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    temporary = parent.child(temporary_name)
    write_error: Exception | None = None
    committed = False
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace_in_directory(parent, temporary_name, name)
        committed = True
    except ReleasePublicationCommittedError as exc:
        write_error = exc
        committed = True
    except Exception as exc:
        write_error = exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception as exc:
            if write_error is None:
                write_error = exc
    if write_error is not None:
        if isinstance(write_error, ReleasePublicationCommittedError):
            raise write_error
        if committed:
            raise ReleasePublicationCommittedError(
                "release_publication_committed_durability_uncertain"
            ) from None
        raise write_error


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a release artifact durably and replace it without a shared tmp name."""
    # Keep the lexical final component. Following a target symlink here would
    # turn an atomic replacement into a write outside the validated parent.
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    with pinned_directory(path.parent) as parent:
        atomic_write_bytes_in_directory(parent, path.name, data)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, canonical_json(payload) + b"\n")


def atomic_write_release_json(path: Path, payload: Any, *, label: str) -> None:
    """Publish JSON only after scanning the exact canonical bytes."""
    resolved = safe_resolve_release_path(path, label=f"{label}_path")
    data = canonical_json(payload) + b"\n"
    assert_no_release_secrets(data, label=label)
    publication_error: ReleaseEvidenceError | None = None
    try:
        atomic_write_bytes(resolved, data)
    except ReleasePublicationCommittedError:
        raise
    except Exception as exc:
        publication_error = safe_release_exception(
            "release_publication_failed",
            exc,
            component=label,
        )
    if publication_error is not None:
        raise publication_error from None


def atomic_write_release_text(path: Path, value: str, *, label: str) -> None:
    """Publish UTF-8 text only after scanning the exact bytes."""
    resolved = safe_resolve_release_path(path, label=f"{label}_path")
    data = value.encode("utf-8")
    assert_no_release_secrets(data, label=label)
    publication_error: ReleaseEvidenceError | None = None
    try:
        atomic_write_bytes(resolved, data)
    except ReleasePublicationCommittedError:
        raise
    except Exception as exc:
        publication_error = safe_release_exception(
            "release_publication_failed",
            exc,
            component=label,
        )
    if publication_error is not None:
        raise publication_error from None


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=config.PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def git_commit() -> str:
    return _git("rev-parse", "HEAD")


def release_git_snapshot(base_ref: str = TRUSTED_RELEASE_BASE_REF) -> dict[str, Any]:
    """Resolve the trusted base and current HEAD independently of producer evidence."""
    if base_ref != TRUSTED_RELEASE_BASE_REF:
        raise ValueError(f"untrusted release base ref: {base_ref!r}")
    base_commit = _git("rev-parse", "--verify", f"{base_ref}^{{commit}}")
    merge_base = _git("merge-base", base_ref, "HEAD")
    head_commit = _git("rev-parse", "--verify", "HEAD^{commit}")
    for label, value in (
        ("base_commit", base_commit),
        ("merge_base", merge_base),
        ("head_commit", head_commit),
    ):
        if not value or not re.fullmatch(r"[0-9a-fA-F]+", value):
            raise ValueError(f"invalid git object id: {label}")
    return {
        "base_ref": base_ref,
        "base_commit": base_commit,
        "merge_base": merge_base,
        "head_commit": head_commit,
        "project_root": str(PROJECT_ROOT.resolve()),
        "diff_check_argv": ["git", "diff", "--check", f"{merge_base}..HEAD", "--"],
    }


def git_tree_sha256() -> str:
    """Return the immutable commit tree object for the release commit."""
    return _git("rev-parse", "HEAD^{tree}")


def git_status_porcelain() -> str:
    """Return the complete porcelain status used by the release gate."""
    return _git("status", "--porcelain=v1", "--untracked-files=all")


def assert_clean_release_tree() -> None:
    """Reject release evidence generation from a dirty checkout.

    A content hash is useful for diagnostics, but it is not a substitute for a
    clean release commit: otherwise a later report can be re-stamped onto a
    different tree.  Development callers may still use ``working_tree_sha256``
    directly; release producers must call this guard before stamping.
    """
    status = git_status_porcelain()
    if status:
        raise RuntimeError("release evidence requires a clean checkout: " + status)


def working_tree_sha256() -> str:
    """Hash tracked changes and *contents* of every non-ignored untracked file.

    The digest contains only SHA-256 values for untracked file contents, never
    their raw bytes.  This makes a dirty-tree identity useful for pre-commit
    verification while still detecting edits to release-critical untracked
    scripts/tests.
    """
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=config.PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=config.PROJECT_ROOT,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    ).stdout
    records: list[dict[str, str]] = []
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        relative = os.fsdecode(raw_path)
        path = (config.PROJECT_ROOT / relative).resolve()
        path_escape_error: RuntimeError | None = None
        try:
            path.relative_to(config.PROJECT_ROOT.resolve())
        except ValueError:
            path_escape_error = RuntimeError(
                f"untracked path escapes project root: {relative}"
            )
        if path_escape_error is not None:
            raise path_escape_error
        if not path.is_file():
            raise RuntimeError(f"untracked release path is not a regular file: {relative}")
        records.append(
            {
                "path": Path(relative).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    records.sort(key=lambda item: item["path"])
    return sha256_bytes(diff + b"\0UNTRACKED\0" + canonical_json(records))


def runtime_identity() -> dict[str, Any]:
    return {
        "llm_profile": config.LLM_PROFILE,
        "enrichment_model": llm_backend.active_model_for("enrichment"),
        "llm_runtime": {
            "timeout_seconds": config.LLM_TIMEOUT_SECONDS,
            "max_async": config.LLM_MAX_ASYNC,
            "delay_seconds": config.LLM_DELAY_SECONDS,
            "reasoning_effort": config.LLM_REASONING_EFFORT,
            "codex_luna_model": config.CODEX_LUNA_MODEL,
            "codex_luna_reasoning_effort": config.CODEX_LUNA_REASONING_EFFORT,
            "codex_timeout_seconds": config.CODEX_LLM_TIMEOUT_SECONDS,
            "codex_max_concurrency": config.CODEX_LLM_MAX_CONCURRENCY,
            "codex_fallback_to_api": config.CODEX_FALLBACK_TO_API,
        },
        "enrichment_runtime": {
            "concurrency": config.ENRICHMENT_CONCURRENCY,
            "timeout_seconds": config.LLM_TIMEOUT_SECONDS,
            "prompt_version": config.ENRICHMENT_PROMPT_VERSION,
            "youtube_prompt_version": config.YOUTUBE_ENRICHMENT_PROMPT_VERSION,
        },
        "rag_runtime": {
            "insert_timeout_seconds": config.RAG_INSERT_TIMEOUT_SECONDS,
            "delete_timeout_seconds": config.RAG_DELETE_TIMEOUT_SECONDS,
            "finalize_timeout_seconds": config.RAG_FINALIZE_TIMEOUT_SECONDS,
        },
        "cold_graph": {
            "batch_size_default": COLD_GRAPH_BATCH_SIZE_DEFAULT,
            "batch_size_max": COLD_GRAPH_BATCH_SIZE_MAX,
            "batch_timeout_seconds_default": COLD_GRAPH_BATCH_TIMEOUT_SECONDS_DEFAULT,
            "rag_runtime": {
                "insert_timeout_seconds": config.RAG_INSERT_TIMEOUT_SECONDS,
                "delete_timeout_seconds": config.RAG_DELETE_TIMEOUT_SECONDS,
                "finalize_timeout_seconds": config.RAG_FINALIZE_TIMEOUT_SECONDS,
            },
        },
        "media_contract_version": MEDIA_CONTRACT_VERSION,
        "late_fusion_enabled": config.LATE_FUSION_ENABLED,
        "reranker_enabled": config.RERANKER_ENABLED,
        "wiki_enabled": config.WIKI_ENABLED,
        "transcription_model": config.TRANSCRIPTION_MODEL,
        "transcription_enabled": config.TRANSCRIPTION_ENABLED,
        "media_vision_backend": config.MEDIA_VISION_BACKEND,
        "ytdlp_require_current": config.YTDLP_REQUIRE_CURRENT,
        "query": {
            "model": config.QUERY_MODEL,
            "base_url": config.QUERY_BASE_URL,
            "max_tokens": config.QUERY_MAX_TOKENS,
            "timeout_seconds": config.QUERY_TIMEOUT_SECONDS,
            "delay_seconds": config.QUERY_DELAY_SECONDS,
            "hybrid_cards_enabled": config.HYBRID_QUERY_CARDS_ENABLED,
            "hybrid_cards_top_k": config.HYBRID_QUERY_CARDS_TOP_K,
            "hybrid_wiki_enabled": config.HYBRID_QUERY_WIKI_ENABLED,
            "hybrid_wiki_top_k": config.HYBRID_QUERY_WIKI_TOP_K,
        },
        "embedding": {
            "base_url": config.EMBEDDING_BASE_URL,
            "model": config.EMBEDDING_MODEL,
            "dimension": config.EMBEDDING_DIM,
            "timeout_seconds": config.EMBEDDING_TIMEOUT_SECONDS,
            "batch_size": config.EMBEDDING_BATCH_SIZE,
            "max_attempts": config.EMBEDDING_MAX_ATTEMPTS,
            "concurrency": config.EMBEDDING_CONCURRENCY,
            "api_configured": bool(config.EMBEDDING_API_KEY),
        },
        "late_fusion": {
            "card_top_k": config.LATE_FUSION_CARD_TOP_K,
            "youtube_top_k": config.LATE_FUSION_YOUTUBE_TOP_K,
            "max_sources": config.LATE_FUSION_MAX_SOURCES,
            "max_input_tokens": config.LATE_FUSION_MAX_INPUT_TOKENS,
            "runtime_context_limit": config.LATE_FUSION_RUNTIME_CONTEXT_LIMIT,
            "fts_timeout_seconds": config.LATE_FUSION_FTS_TIMEOUT_SECONDS,
            "output_token_reserve": config.LATE_FUSION_OUTPUT_TOKEN_RESERVE,
        },
        "reranker": {
            "provider": config.RERANKER_PROVIDER,
            "model": config.RERANKER_MODEL,
            "base_url": config.RERANKER_BASE_URL,
            "top_n": config.RERANKER_TOP_N,
            "candidate_pool": config.RERANKER_CANDIDATE_POOL,
            "api_configured": bool(config.RERANKER_API_KEY),
        },
    }


def effective_cold_runtime(batch_size: int, batch_timeout_seconds: float) -> dict[str, Any]:
    """Return the effective cold-run contract, including explicit overrides."""
    runtime = runtime_identity()
    defaults = runtime["cold_graph"]
    timeout = float(batch_timeout_seconds)
    return {
        "batch_size": batch_size,
        "batch_timeout_seconds": timeout,
        "batch_size_default": defaults["batch_size_default"],
        "batch_size_max": defaults["batch_size_max"],
        "batch_timeout_seconds_default": defaults["batch_timeout_seconds_default"],
        "batch_size_override": batch_size != defaults["batch_size_default"],
        "batch_timeout_override": timeout != defaults["batch_timeout_seconds_default"],
        "override": (
            batch_size != defaults["batch_size_default"] or timeout != defaults["batch_timeout_seconds_default"]
        ),
        "rag_runtime": dict(runtime["rag_runtime"]),
        "config_identity_sha256": sha256_bytes(canonical_json(runtime)),
    }


def config_identity_sha256() -> str:
    return sha256_bytes(canonical_json(runtime_identity()))


def corpus_identity_sha256(manifest: dict[str, Any]) -> str:
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list):
        raise ValueError("manifest.sources must be a list")
    rows: list[dict[str, Any]] = []
    for entry in sources:
        if not isinstance(entry, dict):
            raise ValueError("manifest source entry must be an object")
        rows.append(
            {
                "source_id": entry.get("source_id"),
                "normalized_path": entry.get("normalized_path"),
                "card_path": entry.get("card_path"),
                "normalized_sha256": entry.get("normalized_sha256"),
                "enriched_sha256": entry.get("enriched_sha256"),
                "graph_text_sha256": entry.get("graph_text_sha256"),
            }
        )
    rows.sort(key=lambda item: (str(item["source_id"]), str(item["normalized_path"])))
    return sha256_bytes(canonical_json(rows))


def build_identity(
    release_run_id: str,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    manifest_bytes: bytes | None = None,
) -> dict[str, Any]:
    manifest_path = safe_resolve_release_path(
        manifest_path,
        label="release_manifest_path",
    )
    if manifest_bytes is None:
        validated_manifest_bytes = manifest_path.read_bytes()
    else:
        if not isinstance(manifest_bytes, bytes):
            raise ValueError("manifest snapshot must be bytes")
        current_manifest_bytes = manifest_path.read_bytes()
        if current_manifest_bytes != manifest_bytes:
            raise ValueError("manifest bytes changed after validation")
        validated_manifest_bytes = manifest_bytes
    manifest_from_bytes = strict_json_loads(validated_manifest_bytes)
    if not isinstance(manifest_from_bytes, dict):
        raise ValueError("manifest root must be an object")
    if canonical_json(manifest_from_bytes) != canonical_json(manifest):
        raise ValueError("manifest bytes do not match validated payload")
    validate_manifest_run_id(release_run_id, manifest_from_bytes)
    runtime = runtime_identity()
    return {
        "release_run_id": release_run_id,
        "git_commit": git_commit(),
        "git_tree_sha256": git_tree_sha256(),
        "working_tree_sha256": working_tree_sha256(),
        "config_identity_sha256": sha256_bytes(canonical_json(runtime)),
        "runtime_identity": runtime,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_bytes(validated_manifest_bytes),
        "corpus_identity_sha256": corpus_identity_sha256(manifest_from_bytes),
    }


def validate_manifest_run_id(release_run_id: Any, manifest: dict[str, Any]) -> str:
    """Fail closed when a report run id is not the manifest's own identity.

    Release evidence is immutable only when the run id is taken from the
    already-created accepted manifest.  Keeping this check in the shared
    identity builder prevents individual report producers from accidentally
    stamping a different run id onto the same manifest.
    """
    if not isinstance(release_run_id, str) or not release_run_id.strip():
        raise ValueError("release_run_id must be non-empty string")
    expected = release_run_id.strip()
    actual_value = manifest.get("run_id") if isinstance(manifest, dict) else None
    if not isinstance(actual_value, str) or not actual_value.strip():
        raise ValueError("manifest run_id must be non-empty string")
    actual = actual_value.strip()
    if actual != expected:
        raise ValueError("manifest_run_id_mismatch")
    assert_no_release_secrets(expected, label="release_run_id")
    assert_no_release_secrets(actual, label="manifest_run_id")
    return actual


@dataclass(frozen=True)
class ReleasePreflight:
    """Immutable accepted-manifest snapshot validated before producer work."""

    manifest_path: Path
    manifest: dict[str, Any]
    manifest_bytes: bytes
    identity: dict[str, Any]


def preflight_release_identity(
    release_run_id: str,
    manifest_path: Path,
    *,
    validation: Any | None = None,
    identity_builder: Any | None = None,
) -> ReleasePreflight:
    """Validate manifest, run binding and identity before any producer side effect."""
    from geospoiler.retrieval.lightrag.manifest_contract import load_manifest_file

    resolved_manifest = safe_resolve_release_path(
        manifest_path,
        label="release_manifest_path",
    )
    if validation is None:
        validation = load_manifest_file(resolved_manifest)
    if validation.errors or validation.manifest_bytes is None:
        raise ValueError("accepted manifest validation failed")
    builder = identity_builder or build_identity
    identity = builder(
        release_run_id,
        resolved_manifest,
        validation.payload,
        manifest_bytes=validation.manifest_bytes,
    )
    assert_no_release_secrets(canonical_json(identity), label="release_identity")
    return ReleasePreflight(
        manifest_path=resolved_manifest,
        manifest=validation.payload,
        manifest_bytes=validation.manifest_bytes,
        identity=identity,
    )


def _payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "payload_sha256"}
    return sha256_bytes(canonical_json(unsigned))


def verify_payload_sha256(payload: dict[str, Any]) -> bool:
    expected = payload.get("payload_sha256")
    return isinstance(expected, str) and expected == _payload_sha256(payload)


def stamp_report(
    payload: dict[str, Any],
    identity: dict[str, Any],
    command: str,
    *,
    producer: str,
    producer_schema_version: str,
    source_inputs: dict[str, Any],
    command_argv: list[str],
) -> dict[str, Any]:
    """Stamp a producer report once with a self-verifying evidence envelope.

    The keyword arguments are intentionally explicit for release producers.
    Release producers must provide every identity-critical argument explicitly.
    """
    if not isinstance(payload, dict):
        raise TypeError("release report payload must be an object")
    stamped = dict(payload)
    stamped["release_schema_version"] = RELEASE_EVIDENCE_SCHEMA
    stamped["release_identity"] = dict(identity)
    stamped["command"] = command
    if not producer or not producer_schema_version or not source_inputs or not command_argv:
        raise ValueError("producer, schema, source_inputs and command_argv are required")
    stamped["producer"] = producer
    stamped["producer_schema_version"] = producer_schema_version
    stamped["generated_at"] = datetime.now(UTC).isoformat()
    stamped["effective_runtime_identity"] = dict(identity.get("runtime_identity") or {})
    stamped["source_inputs"] = dict(source_inputs)
    stamped["command_argv"] = list(command_argv)
    stamped["errors"] = list(payload.get("errors") or [])
    if not isinstance(stamped["errors"], list):
        raise ValueError("release report errors must be a list")
    assert_no_release_secrets(canonical_json(stamped), label="release_report")
    stamped["payload_sha256"] = _payload_sha256(stamped)
    return stamped


def validate_report_envelope(
    payload: dict[str, Any],
    *,
    kind: str | None = None,
    identity: dict[str, Any] | None = None,
    allow_errors: bool = False,
) -> list[str]:
    """Return deterministic envelope violations shared by builder/verifier."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["report_not_object"]
    contract = REPORT_CONTRACTS.get(kind or "") if kind else None
    if contract is not None:
        if payload.get("producer") != contract["producer"]:
            errors.append(f"producer_invalid:{kind}")
        if payload.get("producer_schema_version") != contract["schema_version"]:
            errors.append(f"producer_schema_invalid:{kind}")
        if payload.get("schema_version") != contract["schema_version"]:
            errors.append(f"payload_schema_invalid:{kind}")
        required_payload = set(contract["required_payload"])
        allowed_payload = set(contract["allowed_payload"])
        missing_payload = sorted(required_payload - set(payload))
        errors.extend(f"payload_missing:{kind}:{field}" for field in missing_payload)
        unknown_payload = sorted(
            field
            for field in payload
            if field not in allowed_payload
            and field
            not in {
                "release_schema_version",
                "release_identity",
                "command",
                "producer",
                "producer_schema_version",
                "generated_at",
                "effective_runtime_identity",
                "source_inputs",
                "command_argv",
                "payload_sha256",
            }
        )
        errors.extend(f"payload_unknown:{kind}:{field}" for field in unknown_payload)
        for field, expected_type in contract["field_types"].items():
            if field not in payload:
                continue
            value = payload[field]
            valid = {
                "str": isinstance(value, str),
                "str_or_none": value is None or isinstance(value, str),
                "sha256": _is_sha256(value),
                "sha256_or_none": value is None or _is_sha256(value),
                "int": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "bool": isinstance(value, bool),
                "bool_or_none": value is None or isinstance(value, bool),
                "list": isinstance(value, list),
                "dict": isinstance(value, dict),
                "dict_or_none": value is None or isinstance(value, dict),
            }.get(expected_type, False)
            if not valid:
                errors.append(f"payload_type_invalid:{kind}:{field}:{expected_type}")
            elif field in _FIELD_ENUMS and value is not None and value not in _FIELD_ENUMS[field]:
                errors.append(f"payload_enum_invalid:{kind}:{field}:{value}")
    required = {
        "release_schema_version",
        "release_identity",
        "command",
        "producer",
        "producer_schema_version",
        "generated_at",
        "effective_runtime_identity",
        "source_inputs",
        "command_argv",
        "payload_sha256",
        "errors",
    }
    missing = sorted(required - set(payload))
    errors.extend(f"envelope_missing:{field}" for field in missing)
    allowed_envelope = required | {"schema_version", "status"}
    unknown_critical = sorted(
        key for key in payload
        if (key.startswith("release_") or key in {"producer", "producer_schema_version", "effective_runtime_identity", "source_inputs", "command_argv"})
        and key not in allowed_envelope
    )
    errors.extend(f"envelope_unknown:{field}" for field in unknown_critical)
    if payload.get("release_schema_version") != RELEASE_EVIDENCE_SCHEMA:
        errors.append("release_schema_invalid")
    if not isinstance(payload.get("producer"), str) or not payload.get("producer"):
        errors.append("producer_missing")
    if not isinstance(payload.get("producer_schema_version"), str) or not payload.get("producer_schema_version"):
        errors.append("producer_schema_missing")
    generated = payload.get("generated_at")
    if not isinstance(generated, str):
        errors.append("generated_at_invalid")
    else:
        try:
            parsed = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
                errors.append("generated_at_not_utc")
        except ValueError:
            errors.append("generated_at_invalid")
    argv = payload.get("command_argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        errors.append("command_argv_invalid")
    if not isinstance(payload.get("command"), str) or not payload.get("command"):
        errors.append("command_invalid")
    if not isinstance(payload.get("errors"), list):
        errors.append("errors_invalid")
    elif payload["errors"] and not allow_errors:
        errors.append("report_contains_errors")
    source_inputs = payload.get("source_inputs")
    if not isinstance(source_inputs, dict):
        errors.append("source_inputs_invalid")
    else:
        if set(source_inputs) != {"manifest_sha256", "corpus_identity_sha256"}:
            errors.append("source_inputs_unknown_fields")
        for field in ("manifest_sha256", "corpus_identity_sha256"):
            if not _is_sha256(source_inputs.get(field)):
                errors.append(f"source_inputs_invalid:{field}")
    runtime = payload.get("effective_runtime_identity")
    if not isinstance(runtime, dict):
        errors.append("effective_runtime_identity_invalid")
    report_identity = payload.get("release_identity")
    if not isinstance(report_identity, dict):
        errors.append("release_identity_invalid")
    if identity is not None and report_identity != identity:
        errors.append("release_identity_mismatch")
    if isinstance(report_identity, dict) and isinstance(source_inputs, dict):
        if source_inputs.get("manifest_sha256") != report_identity.get("manifest_sha256"):
            errors.append("source_inputs_manifest_mismatch")
        if source_inputs.get("corpus_identity_sha256") != report_identity.get("corpus_identity_sha256"):
            errors.append("source_inputs_corpus_mismatch")
        if runtime != report_identity.get("runtime_identity"):
            errors.append("effective_runtime_mismatch")
    if not verify_payload_sha256(payload):
        errors.append("payload_sha_mismatch")
    return errors


def evidence_manifest(identity: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": RELEASE_EVIDENCE_SCHEMA,
        "release_identity": dict(identity),
        "reports": reports,
    }
