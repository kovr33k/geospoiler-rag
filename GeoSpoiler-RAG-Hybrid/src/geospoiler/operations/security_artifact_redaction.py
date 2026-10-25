"""Audit and redact sensitive URL query values in historical artifacts.

The tool never prints sensitive values.  Apply mode preserves byte-for-byte
originals under an external quarantine root before replacing retained copies.
SQLite files are changed only through SQL updates inside a transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from geospoiler.config import PROJECT_ROOT as PROJECT_ROOT
from geospoiler.operations.release_identity import (
    ReleasePublicationCommittedError,
    SafeArgumentParser,
    assert_no_release_secrets,
    atomic_write_bytes,
    atomic_write_bytes_in_directory,
    canonical_json,
    durable_replace_in_directory,
    durable_unlink_in_directory,
    pinned_directory,
    read_pinned_file_bytes,
    run_release_cli,
    strict_json_loads,
)

DEFAULT_ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
DEFAULT_REPORT_DIR = DEFAULT_ARTIFACTS_ROOT / "security_artifact_redaction_20260812"
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "accesstoken",
    "api_key",
    "apikey",
    "auth",
    "key",
    "signature",
    "sig",
    "token",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
RAW_TOKEN_RE = re.compile(
    r"(?i)(?P<key>access[-_]?token)(?P<sep>\s*=\s*)(?P<value>[^&\s\"'<>]+)"
)
BYTE_TOKEN_RE = re.compile(rb"(?i)access[-_]?token\s*=\s*([^&\s\"'<>]+)")
REDACTED_BYTE_VALUES = {b"[redacted]", b"%5bredacted%5d"}
REDACTED_VALUES = {"[redacted]", "%5bredacted%5d"}
SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class Finding:
    path: str
    file_type: str
    size: int
    sha256: str
    occurrences: int
    retention_class: str
    rotation_status: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        raise RuntimeError("artifact path boundary violation") from None
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return (
        stat.S_ISLNK(metadata.st_mode)
        or path.is_symlink()
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _validated_artifact_root(root: Path) -> Path:
    lexical_root = root.absolute()
    current = Path(lexical_root.anchor)
    for part in lexical_root.parts[1:]:
        current /= part
        if _is_link_or_reparse(current):
            raise RuntimeError("artifact path boundary violation")
    try:
        resolved = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("artifact path boundary violation") from None
    if not resolved.is_dir():
        raise RuntimeError("artifact path boundary violation")
    return resolved


def _validated_destination_root(root: Path) -> Path:
    """Create a destination root one component at a time without following links."""
    lexical_root = root.absolute()
    current = Path(lexical_root.anchor)
    for part in lexical_root.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            current.mkdir()
        except OSError:
            raise RuntimeError("artifact destination boundary violation") from None
        if _is_link_or_reparse(current) or not current.is_dir():
            raise RuntimeError("artifact destination boundary violation")
    try:
        return lexical_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeError("artifact destination boundary violation") from None


def _validated_destination_candidate(root: Path, relative: Path) -> Path:
    """Return an in-root destination after validating/creating every parent."""
    if relative.is_absolute() or relative.drive or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise RuntimeError("artifact destination boundary violation")
    root = _validated_destination_root(root)
    parent = root
    for part in relative.parts[:-1]:
        parent /= part
        try:
            parent.lstat()
        except FileNotFoundError:
            parent.mkdir()
        except OSError:
            raise RuntimeError("artifact destination boundary violation") from None
        if _is_link_or_reparse(parent) or not parent.is_dir():
            raise RuntimeError("artifact destination boundary violation")
    target = parent / relative.name
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise RuntimeError("artifact destination boundary violation") from None
    else:
        if _is_link_or_reparse(target) or not target.is_file():
            raise RuntimeError("artifact destination boundary violation")
    try:
        target.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("artifact destination boundary violation") from None
    return target


@contextmanager
def _locked_destination_candidate(
    root: Path, relative: Path, *, lock_existing_target: bool = False
) -> Iterator[tuple[Path, bool]]:
    """Keep validated Windows parents unreplaceable until the write is complete."""
    target = _validated_destination_candidate(root, relative)
    target_existed = target.exists()
    with pinned_directory(target.parent):
        target = _validated_destination_candidate(root, relative)
        if lock_existing_target and target.exists() != target_existed:
            raise RuntimeError("artifact destination boundary violation")
        yield target, target_existed


def _validated_artifact_candidate(root: Path, candidate: Path) -> Path:
    """Reject links/reparse points and return a resolved in-root candidate."""
    lexical_root = root.absolute()
    lexical_candidate = candidate.absolute()
    try:
        relative = lexical_candidate.relative_to(lexical_root)
    except ValueError:
        raise RuntimeError("artifact path boundary violation") from None
    current = lexical_root
    if _is_link_or_reparse(current):
        raise RuntimeError("artifact path boundary violation")
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise RuntimeError("artifact path boundary violation")
    try:
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical_candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("artifact path boundary violation") from None
    return resolved


def _is_unredacted(value: str) -> bool:
    return value.strip().casefold() not in REDACTED_VALUES


def _redact_url(value: str) -> str:
    """Mirror normalizer.youtube_handler.redact_sensitive_url semantics."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    filtered = [
        (
            key,
            "[REDACTED]"
            if key.casefold().replace("-", "_") in SENSITIVE_QUERY_KEYS
            else item,
        )
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), ""))


def redact_sensitive_text(value: str) -> str:
    def redact_url_match(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ").,;]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return _redact_url(raw) + suffix

    redacted = URL_RE.sub(redact_url_match, str(value))

    def redact_raw_match(match: re.Match[str]) -> str:
        if not _is_unredacted(match.group("value")):
            return match.group(0)
        return f"{match.group('key')}{match.group('sep')}[REDACTED]"

    return RAW_TOKEN_RE.sub(redact_raw_match, redacted)


def count_unredacted_text(value: str) -> int:
    return sum(
        1
        for match in RAW_TOKEN_RE.finditer(str(value))
        if _is_unredacted(match.group("value"))
    )


def _count_unredacted_bytes(data: bytes) -> int:
    return sum(
        1
        for match in BYTE_TOKEN_RE.finditer(data)
        if not any(match.group(1).strip().lower().startswith(marker) for marker in REDACTED_BYTE_VALUES)
    )


def _decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1251"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("unsupported text encoding")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_text_columns(conn: sqlite3.Connection) -> Iterable[tuple[str, str]]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})"):
            column = str(row[1])
            if column:
                yield str(table), column


def _sqlite_occurrences(path: Path) -> int:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    total = 0
    try:
        for table, column in _sqlite_text_columns(conn):
            qtable = _quote_identifier(table)
            qcolumn = _quote_identifier(column)
            query = (
                f"SELECT {qcolumn} FROM {qtable} "
                f"WHERE typeof({qcolumn})='text'"
            )
            for (value,) in conn.execute(query):
                total += count_unredacted_text(str(value))
    finally:
        conn.close()
    return total


def _file_occurrences(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header == SQLITE_HEADER:
        return "sqlite", max(_sqlite_occurrences(path), _count_unredacted_bytes(path.read_bytes()))
    text, _encoding = _decode_text(path.read_bytes())
    return "text", count_unredacted_text(text)


def _retention_class(relative: Path) -> str:
    folded = relative.as_posix().casefold()
    if any(part in folded for part in ("/backup/", "/before/", "production_backup", "retired")):
        return "historical_backup"
    if "canary" in folded or "pilot" in folded:
        return "historical_evaluation"
    if relative.parent == Path(".") or relative.parent == Path(""):
        return "publishable_report"
    return "historical_runtime_snapshot"


def scan(root: Path) -> list[Finding]:
    root = _validated_artifact_root(root)
    findings: list[Finding] = []
    for lexical_path in sorted(root.rglob("*")):
        path = _validated_artifact_candidate(root, lexical_path)
        if not path.is_file():
            continue
        if path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        try:
            file_type, occurrences = _file_occurrences(path)
        except (OSError, UnicodeError, sqlite3.DatabaseError):
            continue
        if occurrences <= 0:
            continue
        path = _validated_artifact_candidate(root, lexical_path)
        relative = path.relative_to(root)
        findings.append(
            Finding(
                path=relative.as_posix(),
                file_type=file_type,
                size=path.stat().st_size,
                sha256=_sha256(path),
                occurrences=occurrences,
                retention_class=_retention_class(relative),
                rotation_status="pending_external_rotation_review",
            )
        )
    return findings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    schema = str(payload.get("schema_version") or "security_artifact_redaction")
    data = canonical_json(payload) + b"\n"
    assert_no_release_secrets(data, label=schema)
    atomic_write_bytes(path, data)


def _write_destination_json(root: Path, name: str, payload: dict[str, Any]) -> None:
    with _locked_destination_candidate(root, Path(name)) as (path, _target_existed):
        _write_json(path, payload)


def _copy_to_quarantine(
    source: Path, target: Path, expected_sha: str, *, allow_existing: bool = False
) -> None:
    if allow_existing:
        if _is_link_or_reparse(target) or not target.is_file():
            raise RuntimeError("artifact destination boundary violation")
        if _sha256(target) != expected_sha:
            raise RuntimeError(f"quarantine collision: {target}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            descriptor = -1
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _sha256(target) != expected_sha:
        raise RuntimeError(f"quarantine hash mismatch: {source}")


def _copy_bytes_to_quarantine(
    data: bytes, target: Path, expected_sha: str, *, allow_existing: bool = False
) -> None:
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise RuntimeError("source changed after scan")
    if allow_existing:
        if _is_link_or_reparse(target) or not target.is_file() or _sha256(target) != expected_sha:
            raise RuntimeError("quarantine collision")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target_handle:
            descriptor = -1
            target_handle.write(data)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if _sha256(target) != expected_sha:
        raise RuntimeError("quarantine hash mismatch")


def _redact_text_snapshot(parent: Any, name: str, data: bytes) -> int:
    text, encoding = _decode_text(data)
    redacted = redact_sensitive_text(text)
    replacements = count_unredacted_text(text) - count_unredacted_text(redacted)
    if replacements > 0:
        output_encoding = "utf-8-sig" if encoding == "utf-8-sig" else encoding
        atomic_write_bytes_in_directory(parent, name, redacted.encode(output_encoding))
    return replacements


def _recover_sqlite_transition(parent: Any, name: str) -> str:
    prefix = f".{name}."
    suffix = ".sqlite-transition.json"
    recovery_action = "no_transition_found"
    for marker in sorted(parent.io_path.iterdir(), key=lambda item: item.name.casefold()):
        if not marker.name.startswith(prefix) or not marker.name.endswith(suffix):
            continue
        if _is_link_or_reparse(marker) or not marker.is_file():
            raise RuntimeError("SQLite transition marker boundary violation")
        payload = strict_json_loads(read_pinned_file_bytes(marker))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "sqlite_redaction_transition_v1"
            or payload.get("main_name") != name
            or not isinstance(payload.get("redacted_sha256"), str)
            or not isinstance(payload.get("retired"), dict)
        ):
            raise RuntimeError("SQLite transition marker invalid")
        retired = payload["retired"]
        original_sha256 = payload.get("original_sha256", {})
        allowed_members = {f"{name}-wal", f"{name}-shm", f"{name}-journal"}
        if not all(
            isinstance(member_name, str)
            and member_name in allowed_members
            and isinstance(retired_name, str)
            and Path(retired_name).name == retired_name
            for member_name, retired_name in retired.items()
        ):
            raise RuntimeError("SQLite transition marker members invalid")
        if not isinstance(original_sha256, dict) or not all(
            isinstance(member_name, str)
            and member_name in allowed_members
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for member_name, digest in original_sha256.items()
        ):
            raise RuntimeError("SQLite transition marker hashes invalid")
        main = parent.child(name)
        main_is_redacted = (
            main.is_file()
            and hashlib.sha256(read_pinned_file_bytes(main)).hexdigest()
            == payload["redacted_sha256"]
        )
        recovery_action = (
            "redacted_family_finalized"
            if main_is_redacted
            else "original_family_restored"
        )
        for member_name, retired_name in retired.items():
            member = parent.child(member_name)
            retired_path = parent.child(retired_name)
            member_exists = member.exists()
            retired_exists = retired_path.exists()
            expected_sha256 = original_sha256.get(member_name)

            def assert_original(
                candidate: Path, expected: str | None = expected_sha256
            ) -> None:
                if expected is not None and (
                    hashlib.sha256(read_pinned_file_bytes(candidate)).hexdigest()
                    != expected
                ):
                    raise RuntimeError("SQLite transition member hash mismatch")

            if main_is_redacted:
                if member_exists:
                    if expected_sha256 is None:
                        raise RuntimeError("SQLite transition recovery state ambiguous")
                    assert_original(member)
                    durable_unlink_in_directory(parent, member_name)
                if retired_exists:
                    assert_original(retired_path)
                    durable_unlink_in_directory(parent, retired_name)
                recovery_action = "redacted_family_finalized"
                continue
            if member_exists:
                assert_original(member)
                if retired_exists:
                    assert_original(retired_path)
                    durable_unlink_in_directory(parent, retired_name)
                continue
            if not retired_exists:
                raise RuntimeError("SQLite transition recovery member missing")
            assert_original(retired_path)
            durable_replace_in_directory(parent, retired_name, member_name)
            recovery_action = "original_family_restored"
        durable_unlink_in_directory(parent, marker.name)
    return recovery_action


def _sqlite_family_bytes(parent: Any, name: str) -> dict[str, bytes]:
    _recover_sqlite_transition(parent, name)
    family: dict[str, bytes] = {}
    for member_name in (name, f"{name}-wal", f"{name}-shm", f"{name}-journal"):
        member = parent.child(member_name)
        if not member.exists():
            continue
        if _is_link_or_reparse(member) or not member.is_file():
            raise RuntimeError("SQLite family boundary violation")
        family[member_name] = read_pinned_file_bytes(member)
    if name not in family:
        raise RuntimeError("SQLite main database missing")
    return family


def _redact_sqlite_snapshot(
    parent: Any,
    name: str,
    data: bytes,
    *,
    family: dict[str, bytes] | None = None,
) -> int:
    """Redact a consistent logical SQLite backup, including committed WAL pages."""
    nonce = f"{os.getpid()}.{os.urandom(8).hex()}"
    staging_name = f".{name}.{nonce}.sqlite-stage"
    transition_name = f".{name}.{nonce}.sqlite-transition.json"
    staging = parent.child(staging_name)
    staging.mkdir()
    snapshot = staging / name
    replacements = 0
    retired: dict[str, str] = {
        member_name: f".{member_name}.{nonce}.retired"
        for member_name in (family or {})
        if member_name != name
    }
    transition_written = False
    main_published = False
    try:
        if family is None:
            with snapshot.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            source_path = parent.child(name)
            source = sqlite3.connect(source_path, timeout=0.0)
            try:
                source.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                source.execute("BEGIN IMMEDIATE")
                snapshot_bytes = source.serialize()
                source.rollback()
            finally:
                source.close()
            with snapshot.open("xb") as handle:
                handle.write(snapshot_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        conn = sqlite3.connect(snapshot)
        conn.create_function("redact_sensitive_text", 1, redact_sensitive_text, deterministic=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, column in list(_sqlite_text_columns(conn)):
                qtable = _quote_identifier(table)
                qcolumn = _quote_identifier(column)
                rows = conn.execute(
                    f"SELECT {qcolumn} FROM {qtable} "
                    f"WHERE typeof({qcolumn})='text'"
                ).fetchall()
                replacements += sum(count_unredacted_text(str(row[0])) for row in rows)
                if rows:
                    conn.execute(
                        f"UPDATE {qtable} SET {qcolumn}=redact_sensitive_text({qcolumn}) "
                        f"WHERE typeof({qcolumn})='text'"
                    )
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("PRAGMA journal_mode=DELETE")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError("SQLite integrity check failed")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if _sqlite_occurrences(snapshot) != 0 or _count_unredacted_bytes(snapshot.read_bytes()) != 0:
            raise RuntimeError("SQLite redaction verification failed")
        if family is not None:
            current_family = _sqlite_family_bytes(parent, name)
            stable_members = {
                member_name: member_bytes
                for member_name, member_bytes in family.items()
                if not member_name.endswith("-shm")
            }
            current_stable_members = {
                member_name: member_bytes
                for member_name, member_bytes in current_family.items()
                if not member_name.endswith("-shm")
            }
            # Opening a WAL database legitimately updates lock bytes in SHM.
            # Main/WAL/journal bytes must remain identical to the captured family.
            if current_stable_members != stable_members:
                raise RuntimeError("SQLite family changed after snapshot")
            atomic_write_bytes_in_directory(
                parent,
                transition_name,
                canonical_json(
                    {
                        "schema_version": "sqlite_redaction_transition_v1",
                        "main_name": name,
                        "redacted_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                        "retired": retired,
                        "original_sha256": {
                            member_name: hashlib.sha256(member_bytes).hexdigest()
                            for member_name, member_bytes in current_family.items()
                            if member_name != name
                        },
                    }
                )
                + b"\n",
            )
            transition_written = True
            for member_name, retired_name in retired.items():
                durable_replace_in_directory(parent, member_name, retired_name)
        atomic_write_bytes_in_directory(parent, name, snapshot.read_bytes())
        main_published = True
        for retired_name in retired.values():
            durable_unlink_in_directory(parent, retired_name, missing_ok=True)
        if transition_written:
            durable_unlink_in_directory(parent, transition_name)
            transition_written = False
        return replacements
    except BaseException as exc:
        if family is not None:
            recovery_action = _recover_sqlite_transition(parent, name)
            if recovery_action == "no_transition_found" and main_published:
                recovery_action = "redacted_family_finalized"
            if isinstance(exc, ReleasePublicationCommittedError):
                exc.publication_details = {"recovery_action": recovery_action}
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def apply_remediation(
    root: Path,
    findings: list[Finding],
    quarantine_root: Path,
    report_dir: Path,
) -> dict[str, Any]:
    root = _validated_artifact_root(root)
    quarantine_root = _validated_destination_root(quarantine_root)
    report_dir = _validated_destination_root(report_dir)
    if root == quarantine_root or root in quarantine_root.parents:
        raise ValueError("quarantine root must be outside scanned artifacts root")

    baseline = {
        "schema_version": "security_artifact_inventory_v1",
        "generated_at": _utc_now(),
        "root": str(root),
        "files": len(findings),
        "occurrences": sum(item.occurrences for item in findings),
        "items": [asdict(item) for item in findings],
    }
    _write_destination_json(report_dir, "baseline_inventory.json", baseline)

    records: list[dict[str, Any]] = []
    for finding in findings:
        source = _validated_artifact_candidate(root, root / Path(finding.path))
        with pinned_directory(source.parent) as source_parent:
            pinned_source = source_parent.child(source.name)
            if _is_link_or_reparse(pinned_source) or not pinned_source.is_file():
                raise RuntimeError("artifact path boundary violation")
            sqlite_family = (
                _sqlite_family_bytes(source_parent, source.name)
                if finding.file_type == "sqlite"
                else None
            )
            source_bytes = (
                sqlite_family[source.name]
                if sqlite_family is not None
                else read_pinned_file_bytes(pinned_source)
            )
            if hashlib.sha256(source_bytes).hexdigest() != finding.sha256:
                raise RuntimeError(f"source changed after scan: {finding.path}")
            with _locked_destination_candidate(
                quarantine_root,
                Path(finding.path),
                lock_existing_target=True,
            ) as (quarantine, quarantine_existed):
                _copy_bytes_to_quarantine(
                    source_bytes,
                    quarantine,
                    finding.sha256,
                    allow_existing=quarantine_existed,
                )
                family_records = [
                    {
                        "path": finding.path,
                        "quarantine_path": str(quarantine),
                        "sha256": finding.sha256,
                        "size": len(source_bytes),
                    }
                ]
                if sqlite_family is not None:
                    relative_main = Path(finding.path)
                    for member_name, member_bytes in sqlite_family.items():
                        if member_name == source.name:
                            continue
                        member_relative = relative_main.with_name(member_name)
                        member_sha = hashlib.sha256(member_bytes).hexdigest()
                        with _locked_destination_candidate(
                            quarantine_root,
                            member_relative,
                            lock_existing_target=True,
                        ) as (member_target, member_existed):
                            _copy_bytes_to_quarantine(
                                member_bytes,
                                member_target,
                                member_sha,
                                allow_existing=member_existed,
                            )
                        family_records.append(
                            {
                                "path": member_relative.as_posix(),
                                "quarantine_path": str(member_target),
                                "sha256": member_sha,
                                "size": len(member_bytes),
                            }
                        )
                replacements = (
                    _redact_sqlite_snapshot(
                        source_parent,
                        source.name,
                        source_bytes,
                        family=sqlite_family,
                    )
                    if finding.file_type == "sqlite"
                    else _redact_text_snapshot(source_parent, source.name, source_bytes)
                )
                remaining_type, remaining = _file_occurrences(pinned_source)
                if remaining_type != finding.file_type or remaining != 0:
                    raise RuntimeError(f"redaction verification failed: {finding.path}")
                records.append(
                    {
                        **asdict(finding),
                        "quarantine_path": str(quarantine),
                        "quarantine_sha256": _sha256(quarantine),
                        "after_sha256": _sha256(pinned_source),
                        "replacements": replacements,
                        "unredacted_after": remaining,
                        "status": "redacted_retained_copy",
                        "sqlite_family": family_records if sqlite_family is not None else [],
                    }
                )

    post_findings = scan(root)
    report = {
        "schema_version": "security_artifact_redaction_report_v1",
        "generated_at": _utc_now(),
        "status": "passed" if not post_findings else "failed",
        "root": str(root),
        "quarantine_root": str(quarantine_root),
        "files_before": len(findings),
        "files_after": len(post_findings),
        "occurrences_before": sum(item.occurrences for item in findings),
        "occurrences_after": sum(item.occurrences for item in post_findings),
        "rotation_status": "pending_external_rotation_review",
        "records": records,
        "post_scan": [asdict(item) for item in post_findings],
    }
    _write_destination_json(report_dir, "remediation_report.json", report)
    _write_destination_json(
        report_dir,
        "quarantine_manifest.json",
        {
            "schema_version": "security_quarantine_manifest_v1",
            "generated_at": _utc_now(),
            "quarantine_root": str(quarantine_root),
            "files": [
                member
                for item in records
                for member in (
                    item["sqlite_family"]
                    if item.get("sqlite_family")
                    else [
                        {
                            "path": item["path"],
                            "quarantine_path": item["quarantine_path"],
                            "sha256": item["quarantine_sha256"],
                            "size": item["size"],
                        }
                    ]
                )
            ],
        },
    )
    return report


def build_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("scan", "apply"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ARTIFACTS_ROOT)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--quarantine-root", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    assert_no_release_secrets(str(args.root), label="security_artifact_root")
    assert_no_release_secrets(str(args.report_dir), label="security_artifact_report_dir")
    root = _validated_artifact_root(args.root)
    report_dir = args.report_dir
    quarantine_root = (
        args.quarantine_root
        if args.quarantine_root is not None
        else None
    )
    if quarantine_root is not None:
        assert_no_release_secrets(
            str(quarantine_root), label="security_artifact_quarantine_root"
        )
    findings = scan(root)
    summary = {
        "mode": args.mode,
        "root_identity_sha256": hashlib.sha256(
            str(root).encode("utf-8", errors="strict")
        ).hexdigest(),
        "files": len(findings),
        "occurrences": sum(item.occurrences for item in findings),
        "sqlite_files": sum(item.file_type == "sqlite" for item in findings),
        "text_files": sum(item.file_type == "text" for item in findings),
    }
    if args.mode == "scan":
        print(json.dumps(summary, ensure_ascii=False))
        return 1 if findings else 0
    if quarantine_root is None:
        build_parser().error("apply requires --quarantine-root")
    report = apply_remediation(root, findings, quarantine_root, report_dir)
    print(json.dumps({**summary, "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(run_release_cli(main, component="security_artifact_redaction"))
