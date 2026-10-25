from __future__ import annotations

import asyncio
import json
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import (
    build_release_verification,
    migrate_portable_paths,
    security_artifact_redaction,
    validate_backup_restore,
)
from tests.test_release_evidence_r15 import _looks_like_schema_only_release_producer


def _identity() -> dict[str, str]:
    return {
        "manifest_sha256": "a" * 64,
        "corpus_identity_sha256": "b" * 64,
    }


def _preflight(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        identity=_identity(),
        manifest_path=path,
        manifest={},
        manifest_bytes=b"{}",
    )


@pytest.mark.parametrize("existing", (False, True))
def test_backup_stamp_failure_does_not_publish_unstamped_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    source_manifest = tmp_path / "backup-source.json"
    source_manifest.write_text(
        json.dumps(
            {
                "schema_version": "backup_source_manifest_v1",
                "root": str(root),
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    release_manifest = tmp_path / "release-manifest.json"
    release_manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "backup-report.json"
    sentinel = b"existing-report\n"
    if existing:
        output.write_bytes(sentinel)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_backup_restore.py",
            "--manifest",
            str(source_manifest),
            "--report",
            str(output),
            "--release-manifest",
            str(release_manifest),
            "--release-run-id",
            "run-1",
        ],
    )
    with (
        patch.object(
            validate_backup_restore,
            "preflight_release_identity",
            return_value=_preflight(release_manifest),
        ),
        patch.object(
            validate_backup_restore,
            "stamp_report",
            side_effect=OSError("stamp failed"),
        ),
        pytest.raises(OSError, match="stamp failed"),
    ):
        validate_backup_restore.main()
    assert output.read_bytes() == sentinel if existing else not output.exists()


@pytest.mark.parametrize("mode", ("scan", "verify", "apply"))
@pytest.mark.parametrize("existing", (False, True))
def test_portable_stamp_failure_does_not_publish_unstamped_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    existing: bool,
) -> None:
    release_manifest = tmp_path / "release-manifest.json"
    release_manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "portable-report.json"
    sentinel = b"existing-report\n"
    if existing:
        output.write_bytes(sentinel)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_portable_paths.py",
            mode,
            "--report",
            str(output),
            "--manifest",
            str(release_manifest),
            "--release-run-id",
            "run-1",
        ],
    )
    with (
        patch.object(migrate_portable_paths, "_candidate_files", return_value=[]),
        patch.object(
            migrate_portable_paths,
            "preflight_release_identity",
            return_value=_preflight(release_manifest),
        ),
        patch.object(migrate_portable_paths, "build_identity", return_value=_identity()),
        patch.object(
            migrate_portable_paths,
            "stamp_report",
            side_effect=OSError("stamp failed"),
        ),
        pytest.raises(OSError, match="stamp failed"),
    ):
        migrate_portable_paths.main()
    assert output.read_bytes() == sentinel if existing else not output.exists()


@pytest.mark.parametrize("schema", (None, "unknown_backup_schema_v1"))
def test_backup_source_manifest_requires_explicit_supported_schema(
    tmp_path: Path, schema: str | None
) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    payload: dict[str, object] = {"root": str(root), "files": []}
    if schema is not None:
        payload["schema_version"] = schema
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported backup source manifest schema"):
        validate_backup_restore.validate(manifest, tmp_path / "report.json")


def test_backup_source_manifest_accepts_tagged_legacy_read_only_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "backup_restore_closeout_v1",
                "root": str(root),
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    report = validate_backup_restore.validate(manifest)
    assert report["status"] == "passed"


@pytest.mark.parametrize("producer", ("backup", "portable"))
def test_release_report_is_stamped_before_the_only_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    producer: str,
) -> None:
    release_manifest = tmp_path / "release-manifest.json"
    release_manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "report.json"
    events: list[str] = []

    def stamp(payload, *args, **kwargs):
        del args, kwargs
        events.append("stamp")
        return {**payload, "payload_sha256": "c" * 64}

    def publish(*args, **kwargs):
        del args, kwargs
        events.append("publish")
        raise OSError("publication failed")

    if producer == "backup":
        root = tmp_path / "backup"
        root.mkdir()
        source_manifest = tmp_path / "backup-source.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "backup_source_manifest_v1",
                    "root": str(root),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        argv = [
            "validate_backup_restore.py",
            "--manifest",
            str(source_manifest),
            "--report",
            str(output),
            "--release-manifest",
            str(release_manifest),
            "--release-run-id",
            "run-1",
        ]
        module = validate_backup_restore
    else:
        argv = [
            "migrate_portable_paths.py",
            "scan",
            "--report",
            str(output),
            "--manifest",
            str(release_manifest),
            "--release-run-id",
            "run-1",
        ]
        module = migrate_portable_paths

    monkeypatch.setattr(sys, "argv", argv)
    patches = [
        patch.object(
            module,
            "preflight_release_identity",
            return_value=_preflight(release_manifest),
        ),
        patch.object(module, "stamp_report", side_effect=stamp),
        patch.object(module, "atomic_write_release_json", side_effect=publish),
    ]
    if producer == "portable":
        patches.append(
            patch.object(migrate_portable_paths, "_candidate_files", return_value=[])
        )
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        with pytest.raises(OSError, match="publication failed"):
            module.main()
    assert events == ["stamp", "publish"]
    assert not output.exists()


@pytest.mark.parametrize(
    "source",
    (
        """
import json
def write(path):
    payload = {'schema_version': 'accepted_manifest_v2'}
    path.write_text(json.dumps(payload))
""",
        """
import json
class Writer:
    def write(self, path):
        json.dump({'schema_version': 'accepted_manifest_v2'}, path.open('w'))
""",
        """
def write(path):
    path.write_bytes(b'{\"schema_version\":\"accepted_manifest_v2\"}')
""",
    ),
)
def test_schema_inventory_discovers_non_cli_direct_writer(source: str) -> None:
    assert _looks_like_schema_only_release_producer(source)


def test_schema_inventory_ignores_unknown_schema() -> None:
    source = """
import json
def write(path):
    path.write_text(json.dumps({'schema_version': 'not_release_v1'}))
"""
    assert not _looks_like_schema_only_release_producer(source)


@pytest.mark.parametrize("link_kind", ("file", "parent", "reparse"))
def test_security_scan_rejects_link_or_reparse_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link_kind: str
) -> None:
    root = tmp_path / "artifacts"
    parent = root / "nested"
    parent.mkdir(parents=True)
    candidate = parent / "artifact.txt"
    candidate.write_text("access_token=outside-secret", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    original_lstat = Path.lstat

    def fake_is_symlink(path: Path) -> bool:
        if link_kind == "file" and path == candidate:
            return True
        if link_kind == "parent" and path == parent:
            return True
        return original_is_symlink(path)

    def fake_lstat(path: Path):
        result = original_lstat(path)
        if link_kind == "reparse" and path == candidate:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return result

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with (
        patch.object(
            security_artifact_redaction,
            "_file_occurrences",
            side_effect=AssertionError("external candidate was read"),
        ),
        pytest.raises(RuntimeError, match="artifact path boundary violation"),
    ):
        security_artifact_redaction.scan(root)


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, asyncio.CancelledError))
def test_verifier_failure_receipt_preserves_primary_and_secondary_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    run_dir = tmp_path / "attempt-001"
    run_dir.mkdir()
    marker = "sk-or-v1-" + "X" * 24
    original = interrupt_type(marker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_release_verification.py",
            "--input-dir",
            str(tmp_path / "input"),
            "--run-dir",
            str(run_dir),
            "--release-run-id",
            "run-1",
        ],
    )
    with (
        patch.object(build_release_verification, "build_report", side_effect=original),
        pytest.raises(interrupt_type) as caught,
    ):
        build_release_verification.main()
    assert caught.value is original
    failure_dir = next(tmp_path.glob("attempt-001.failed-*"))
    raw = (failure_dir / "failed_release_verification.json").read_text(
        encoding="utf-8"
    )
    payload = json.loads(raw)
    assert payload["primary_failure"]["exception_type"] == interrupt_type.__name__
    assert payload["primary_failure"]["error_code"] == "verification_interrupted"
    assert payload["secondary_failure"]["exception_type"] == "RuntimeError"
    assert marker not in raw
