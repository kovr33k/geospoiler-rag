"""Crash-recoverable two-file publication for normalized text and metadata."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path


def _fsync_write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability after a rename.

    Windows commonly rejects directory handles; the file fsync remains the
    guaranteed fallback there and the failure is intentionally non-fatal.
    """
    try:
        directory_fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def atomic_write_bytes(path: Path, data: bytes, *, suffix: str = "atomic") -> None:
    """Durably publish one file without exposing a partial payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{suffix}.tmp")
    try:
        _fsync_write(temporary, data)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def recover_atomic_pair(first: Path, second: Path, *, journal_suffix: str) -> bool:
    journal = second.with_name(f".{second.name}.{journal_suffix}.journal")
    if not journal.is_file():
        return False
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("journal root must be an object")
        if payload.get("schema_version") != "atomic_pair_v1":
            raise ValueError("unsupported atomic pair journal schema")
        phase = payload.get("phase")
        if phase not in {"prepared", "first_replaced", "committed"}:
            raise ValueError(f"unsupported journal phase: {phase!r}")
        recorded_first = Path(str(payload.get("first") or "")).resolve()
        recorded_second = Path(str(payload.get("second") or "")).resolve()
        if recorded_first != first.resolve() or recorded_second != second.resolve():
            raise ValueError("journal target paths do not match the requested pair")
        original_first = base64.b64decode(payload["original_first"], validate=True)
        original_second = base64.b64decode(payload["original_second"], validate=True)
        if phase != "committed":
            _fsync_write(first, original_first)
            _fsync_write(second, original_second)
            _fsync_directory(first.parent)
            _fsync_directory(second.parent)
        else:
            expected_first = payload.get("new_first_sha256")
            expected_second = payload.get("new_second_sha256")
            if expected_first and hashlib.sha256(first.read_bytes()).hexdigest() != expected_first:
                raise ValueError("committed first target hash does not match journal")
            if expected_second and hashlib.sha256(second.read_bytes()).hexdigest() != expected_second:
                raise ValueError("committed second target hash does not match journal")
        journal.unlink(missing_ok=True)
    except Exception as exc:
        raise RuntimeError(f"atomic pair recovery failed for {first} / {second}: {exc}") from exc
    return True


def atomic_write_pair(
    first: Path,
    first_bytes: bytes,
    second: Path,
    second_bytes: bytes,
    *,
    original_first: bytes,
    original_second: bytes,
    journal_suffix: str,
) -> None:
    """Publish a pair with a durable rollback journal.

    A crash before the committed marker is recovered to the original pair on
    the next invocation. A crash after both replaces but before journal
    cleanup sees the committed marker and keeps the new pair.
    """
    recover_atomic_pair(first, second, journal_suffix=journal_suffix)
    first_tmp = first.with_name(f".{first.name}.{journal_suffix}.tmp")
    second_tmp = second.with_name(f".{second.name}.{journal_suffix}.tmp")
    journal = second.with_name(f".{second.name}.{journal_suffix}.journal")
    payload = {
        "schema_version": "atomic_pair_v1",
        "phase": "prepared",
        "first": str(first),
        "second": str(second),
        "original_first": base64.b64encode(original_first).decode("ascii"),
        "original_second": base64.b64encode(original_second).decode("ascii"),
        "original_first_sha256": hashlib.sha256(original_first).hexdigest(),
        "original_second_sha256": hashlib.sha256(original_second).hexdigest(),
        "new_first_sha256": hashlib.sha256(first_bytes).hexdigest(),
        "new_second_sha256": hashlib.sha256(second_bytes).hexdigest(),
    }
    try:
        _fsync_write(first_tmp, first_bytes)
        _fsync_write(second_tmp, second_bytes)
        atomic_write_bytes(
            journal,
            json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            suffix=f"{journal_suffix}.journal",
        )
        os.replace(first_tmp, first)
        _fsync_directory(first.parent)
        payload["phase"] = "first_replaced"
        atomic_write_bytes(
            journal,
            json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            suffix=f"{journal_suffix}.journal",
        )
        os.replace(second_tmp, second)
        _fsync_directory(second.parent)
        payload["phase"] = "committed"
        atomic_write_bytes(
            journal,
            json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            suffix=f"{journal_suffix}.journal",
        )
        journal.unlink(missing_ok=True)
    except Exception:
        _fsync_write(first, original_first)
        _fsync_write(second, original_second)
        _fsync_directory(first.parent)
        _fsync_directory(second.parent)
        journal.unlink(missing_ok=True)
        raise
    finally:
        first_tmp.unlink(missing_ok=True)
        second_tmp.unlink(missing_ok=True)
