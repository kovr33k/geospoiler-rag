from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import (
    build_release_test_evidence,
    build_release_verification,
    migrate_portable_paths,
    release_identity,
    security_artifact_redaction,
    validate_backup_restore,
)


def _backup_fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "backup"
    root.mkdir()
    source = root / "item.txt"
    source.write_bytes(b"payload")
    manifest = tmp_path / "backup.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "backup_source_manifest_v1",
                "root": str(root),
                "files": [
                    {
                        "path": source.name,
                        "bytes": source.stat().st_size,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest, source


def test_security_cli_preserves_lexical_root_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("access_token=hidden", encoding="utf-8")
    lexical = tmp_path / "linked-root"
    try:
        lexical.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    monkeypatch.setattr(
        sys,
        "argv",
        ["security_artifact_redaction.py", "scan", "--root", str(lexical)],
    )
    with (
        patch.object(
            security_artifact_redaction,
            "_file_occurrences",
            side_effect=AssertionError("outside file was read"),
        ),
        pytest.raises(RuntimeError, match="boundary violation"),
    ):
        security_artifact_redaction.main()


def test_security_root_rejects_reparse_attribute_before_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    original = security_artifact_redaction._is_link_or_reparse
    monkeypatch.setattr(
        security_artifact_redaction,
        "_is_link_or_reparse",
        lambda path: path == root or original(path),
    )
    with (
        patch.object(
            security_artifact_redaction,
            "_file_occurrences",
            side_effect=AssertionError("reparse root was scanned"),
        ),
        pytest.raises(RuntimeError, match="boundary violation"),
    ):
        security_artifact_redaction.scan(root)


def test_quarantine_link_cannot_escape_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    source = root / "nested" / "item.txt"
    source.parent.mkdir()
    source.write_text("access_token=hidden", encoding="utf-8")
    finding = security_artifact_redaction.scan(root)[0]
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (quarantine / "nested").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(RuntimeError, match="destination boundary violation"):
        security_artifact_redaction.apply_remediation(
            root, [finding], quarantine, tmp_path / "reports"
        )
    assert not (outside / "item.txt").exists()
    assert source.read_text(encoding="utf-8") == "access_token=hidden"
    assert not (tmp_path / "reports" / "remediation_report.json").exists()


def test_quarantine_reparse_component_is_rejected_without_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    source = root / "nested" / "item.txt"
    source.parent.mkdir()
    source.write_text("access_token=hidden", encoding="utf-8")
    finding = security_artifact_redaction.scan(root)[0]
    quarantine = tmp_path / "quarantine"
    (quarantine / "nested").mkdir(parents=True)
    original = security_artifact_redaction._is_link_or_reparse
    monkeypatch.setattr(
        security_artifact_redaction,
        "_is_link_or_reparse",
        lambda path: path == quarantine / "nested" or original(path),
    )
    with pytest.raises(RuntimeError, match="destination boundary violation"):
        security_artifact_redaction.apply_remediation(
            root, [finding], quarantine, tmp_path / "reports"
        )
    assert source.read_text(encoding="utf-8") == "access_token=hidden"


@pytest.mark.parametrize(
    "raw",
    (
        '{"schema_version":"backup_source_manifest_v1","schema_version":"backup_source_manifest_v1","files":[]}',
        '{"schema_version":"backup_source_manifest_v1","files":[],"files":[]}',
        '{"schema_version":"backup_source_manifest_v1","files":[{"path":"a","path":"b"}]}',
        '{"schema_version":"backup_source_manifest_v1","files":[{"path":"a","bytes":1,"bytes":2}]}',
        '{"schema_version":"backup_source_manifest_v1","files":[{"path":"a","sha256":"0","sha256":"1"}]}',
    ),
)
def test_backup_manifest_rejects_duplicate_keys(tmp_path: Path, raw: str) -> None:
    manifest = tmp_path / "backup.json"
    manifest.write_text(raw, encoding="utf-8")
    with pytest.raises(RuntimeError, match="JSON is invalid"):
        validate_backup_restore.validate(manifest)


def test_backup_snapshot_detects_manifest_and_file_mutation(tmp_path: Path) -> None:
    manifest, source = _backup_fixture(tmp_path)
    report = validate_backup_restore.validate(manifest)
    frozen = tmp_path / "frozen.json"
    frozen.write_bytes(manifest.read_bytes())
    errors: list[str] = []
    assert build_release_verification._backup_snapshot_is_fresh(
        report, {"backup_source_manifest": frozen}, errors
    )
    source.write_bytes(b"changed")
    errors = []
    assert not build_release_verification._backup_snapshot_is_fresh(
        report, {"backup_source_manifest": frozen}, errors
    )
    assert "backup_snapshot_revalidation_failed" in errors


def test_backup_snapshot_detects_source_manifest_byte_mutation(tmp_path: Path) -> None:
    manifest, _source = _backup_fixture(tmp_path)
    report = validate_backup_restore.validate(manifest)
    frozen = tmp_path / "frozen.json"
    frozen.write_bytes(manifest.read_bytes())
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    errors: list[str] = []
    assert not build_release_verification._backup_snapshot_is_fresh(
        report, {"backup_source_manifest": frozen}, errors
    )
    assert errors == ["backup_source_manifest_stale"]


def test_portable_inventory_is_deterministic_and_content_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.json"
    first.write_text("safe", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(migrate_portable_paths, "_candidate_files", lambda: [first, second])
    report = migrate_portable_paths.scan()
    assert report["inventory_sha256"] == migrate_portable_paths.candidate_inventory_sha256(
        [second, first]
    )
    first.write_text("changed", encoding="utf-8")
    errors: list[str] = []
    assert not build_release_verification._portable_snapshot_is_fresh(report, errors)
    assert errors == ["portable_snapshot_stale"]


def test_portable_inventory_detects_added_and_deleted_candidates(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    baseline = migrate_portable_paths.candidate_inventory_sha256([first])
    second.write_text("b", encoding="utf-8")
    added = migrate_portable_paths.candidate_inventory_sha256([first, second])
    first.unlink()
    deleted = migrate_portable_paths.candidate_inventory_sha256([second])
    assert len({baseline, added, deleted}) == 3


def test_portable_freshness_rejects_new_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "item.json"
    candidate.write_text('{"card_path":"output/enriched/card.json"}', encoding="utf-8")
    monkeypatch.setattr(migrate_portable_paths, "_candidate_files", lambda: [candidate])
    report = migrate_portable_paths.scan()
    assert report["status"] == "passed"
    candidate.write_text('{"card_path":"Z:/outside/card.json"}', encoding="utf-8")
    errors: list[str] = []
    assert not build_release_verification._portable_snapshot_is_fresh(report, errors)
    assert errors == ["portable_snapshot_stale"]


def test_release_test_subprocesses_all_detach_stdin() -> None:
    tree = ast.parse(Path(build_release_test_evidence.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    assert len(calls) == 3
    for call in calls:
        stdin = next((item.value for item in call.keywords if item.arg == "stdin"), None)
        assert isinstance(stdin, ast.Attribute)
        assert isinstance(stdin.value, ast.Name) and stdin.value.id == "subprocess"
        assert stdin.attr == "DEVNULL"


def test_environment_command_passes_devnull_stdin() -> None:
    completed = subprocess.CompletedProcess(["python", "--version"], 0, "Python 3", "")
    with patch.object(subprocess, "run", return_value=completed) as run:
        assert build_release_test_evidence._run_environment_command(
            ["python", "--version"]
        )["status"] == "passed"
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_atomic_writer_distinguishes_precommit_and_postcommit_failures(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    target.write_bytes(b"old")
    with patch.object(
        release_identity,
        "durable_replace_in_directory",
        side_effect=OSError("replace failed"),
    ):
        with pytest.raises(OSError) as precommit:
            release_identity.atomic_write_bytes(target, b"new")
    assert not getattr(precommit.value, "committed", False)
    assert target.read_bytes() == b"old"

    def commit_then_fail(
        parent: release_identity.PinnedDirectory,
        _source_name: str,
        target_name: str,
    ) -> None:
        parent.child(target_name).write_bytes(b"new")
        raise release_identity.ReleasePublicationCommittedError("directory fsync failed")

    with (
        patch.object(
            release_identity,
            "durable_replace_in_directory",
            side_effect=commit_then_fail,
        ),
        pytest.raises(release_identity.ReleasePublicationCommittedError) as postcommit,
    ):
        release_identity.atomic_write_bytes(target, b"new")
    assert postcommit.value.committed is True
    assert target.read_bytes() == b"new"


def test_release_json_preserves_committed_outcome(tmp_path: Path) -> None:
    with patch.object(
        release_identity,
        "atomic_write_bytes",
        side_effect=release_identity.ReleasePublicationCommittedError("committed"),
    ):
        with pytest.raises(release_identity.ReleasePublicationCommittedError):
            release_identity.atomic_write_release_json(
                tmp_path / "report.json", {"status": "passed"}, label="report"
            )
