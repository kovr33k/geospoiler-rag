from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import release_identity, security_artifact_redaction


def _wal_database(path: Path) -> None:
    code = r"""
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT, note TEXT)")
connection.commit()
connection.execute(
    "INSERT INTO events(value, note) VALUES (?, ?)",
    ("https://example.test/?access_token=REAL_SECRET_VALUE", "keep-me"),
)
connection.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(path)], check=True)


def test_artifact_scan_rejects_secret_clean_secret_aba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("access_token=REAL_SECRET_VALUE", encoding="utf-8")
    stash = tmp_path.parent / f"{tmp_path.name}-candidate-stash.txt"
    original_read = release_identity._read_stable_file_bytes
    calls = 0

    def aba_read(path: Path) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(candidate, stash)
            candidate.write_text("clean", encoding="utf-8")
            try:
                return original_read(candidate)
            finally:
                candidate.unlink()
                os.replace(stash, candidate)
        return original_read(path)

    monkeypatch.setattr(release_identity, "_read_stable_file_bytes", aba_read)

    report = release_identity.scan_release_artifact_tree(tmp_path)

    assert report["status"] == "failed"
    assert report["finding_files"] == 1
    assert "REAL_SECRET_VALUE" in candidate.read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation", ["add", "remove"])
def test_artifact_scan_rejects_scope_add_or_remove_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    original = tmp_path / "original.txt"
    original.write_text("clean", encoding="utf-8")
    added = tmp_path / "added.txt"
    capture = release_identity._artifact_tree_snapshot
    calls = 0

    def mutate_after_snapshot(root: Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        snapshot = capture(root)
        if calls == 1:
            if mutation == "add":
                added.write_text("clean too", encoding="utf-8")
            else:
                original.unlink()
        return snapshot

    monkeypatch.setattr(
        release_identity, "_artifact_tree_snapshot", mutate_after_snapshot
    )

    report = release_identity.scan_release_artifact_tree(tmp_path)

    assert report["status"] == "failed"
    assert {item["error"] for item in report["errors"]} == {
        "artifact_inventory_changed"
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows exact-tree publication contract")
def test_windows_publication_rejects_child_added_after_seal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "validated.txt").write_text("validated", encoding="utf-8")
    target = tmp_path / "target"
    original_rename = release_identity._windows_rename_open_directory
    rename_calls = 0

    with release_identity.prepared_publication_tree(source, target) as publication:
        publication.seal()

        def mutate_then_rename(handle: int, destination: Path) -> None:
            nonlocal rename_calls
            rename_calls += 1
            if rename_calls == 1:
                (source / "attacker.txt").write_text("attacker", encoding="utf-8")
            original_rename(handle, destination)

        with (
            patch.object(
                release_identity,
                "_windows_rename_open_directory",
                side_effect=mutate_then_rename,
            ),
            pytest.raises(release_identity.ReleasePublicationCommittedError) as raised,
        ):
            publication.commit()

    assert publication.committed is True
    assert not target.exists()
    assert release_identity.publication_outcome(raised.value)["recovery_action"] == (
        "committed_publication_quarantined"
    )


def test_posix_publication_rejects_source_swap_inside_rename(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "validated.txt").write_text("validated", encoding="utf-8")
    target = tmp_path / "target"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "attacker.txt").write_text("attacker", encoding="utf-8")
    stranded = tmp_path / "stranded"
    source_metadata = source.stat()
    attacker_metadata = attacker.stat()
    publication = release_identity.PreparedPublicationTree(
        source=source,
        target=target,
        io_path=source,
        parent=release_identity.PinnedDirectory(tmp_path, tmp_path, directory_fd=321),
        source_identity=release_identity._stat_identity(source_metadata),
        source_fd=123,
    )
    publication.seal()
    real_stat = os.stat
    real_fstat = os.fstat

    def fake_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == source.name and kwargs.get("dir_fd") == 321:
            return source_metadata
        if path == target.name and kwargs.get("dir_fd") == 321:
            return attacker_metadata
        return real_stat(path, *args, **kwargs)

    def swap_then_rename(_parent_fd: int, _source_name: str, _target_name: str) -> None:
        if _source_name == source.name:
            os.replace(source, stranded)
            os.replace(attacker, source)
            os.replace(source, target)
        else:
            os.replace(tmp_path / _source_name, tmp_path / _target_name)

    def fake_fstat(descriptor: int) -> os.stat_result:
        if descriptor == 123:
            return source_metadata
        return real_fstat(descriptor)

    with (
        patch.object(release_identity, "os", SimpleNamespace(**{**vars(os), "name": "posix"})),
        patch.object(release_identity.os, "stat", side_effect=fake_stat),
        patch.object(release_identity.os, "fstat", side_effect=fake_fstat),
        patch.object(
            release_identity,
            "_posix_rename_noreplace",
            side_effect=swap_then_rename,
        ),
        patch.object(release_identity.os, "fsync"),
        pytest.raises(release_identity.ReleasePublicationCommittedError) as raised,
    ):
        publication.commit()

    assert publication.committed is True
    assert not target.exists()
    assert release_identity.publication_outcome(raised.value)["recovery_action"] == (
        "committed_publication_quarantined"
    )
    assert (stranded / "validated.txt").is_file()


def test_sqlite_main_commit_then_error_finishes_redacted_family(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = root / "history.sqlite"
    _wal_database(database)
    findings = security_artifact_redaction.scan(root)
    original_write = security_artifact_redaction.atomic_write_bytes_in_directory

    def commit_then_fail(parent: object, name: str, data: bytes) -> None:
        original_write(parent, name, data)
        if name == database.name:
            raise release_identity.ReleasePublicationCommittedError("injected")

    with (
        patch.object(
            security_artifact_redaction,
            "atomic_write_bytes_in_directory",
            side_effect=commit_then_fail,
        ),
        pytest.raises(release_identity.ReleasePublicationCommittedError) as raised,
    ):
        security_artifact_redaction.apply_remediation(
            root,
            findings,
            tmp_path / "quarantine",
            tmp_path / "reports",
        )

    assert release_identity.publication_outcome(raised.value) == {
        "status": "committed_durability_uncertain",
        "committed": True,
        "recovery_action": "redacted_family_finalized",
    }

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value, note FROM events").fetchone() == (
            "https://example.test/?access_token=%5BREDACTED%5D",
            "keep-me",
        )
    finally:
        connection.close()
    assert not (root / "history.sqlite-wal").exists()
    assert not (root / "history.sqlite-shm").exists()
    assert not [
        path
        for path in root.iterdir()
        if "sqlite-stage" in path.name
        or path.name.endswith(".retired")
        or path.name.endswith(".sqlite-transition.json")
    ]


@pytest.mark.parametrize("fault", ["first_retirement", "second_retirement", "main_precommit"])
def test_sqlite_transition_faults_restore_complete_original_family(
    tmp_path: Path, fault: str
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = root / "history.sqlite"
    _wal_database(database)
    findings = security_artifact_redaction.scan(root)

    if fault.startswith(("first", "second")):
        original_replace = security_artifact_redaction.durable_replace_in_directory
        fail_at = 1 if fault == "first_retirement" else 2
        calls = 0

        def retire_then_fail(
            parent: object, source_name: str, target_name: str
        ) -> None:
            nonlocal calls
            calls += 1
            original_replace(parent, source_name, target_name)
            if calls == fail_at:
                raise release_identity.ReleasePublicationCommittedError("injected")

        fault_patch = patch.object(
            security_artifact_redaction,
            "durable_replace_in_directory",
            side_effect=retire_then_fail,
        )
        expected_exception = release_identity.ReleasePublicationCommittedError
    else:
        original_write = security_artifact_redaction.atomic_write_bytes_in_directory

        def fail_before_main(parent: object, name: str, data: bytes) -> None:
            if name == database.name:
                raise OSError("injected main precommit failure")
            original_write(parent, name, data)

        fault_patch = patch.object(
            security_artifact_redaction,
            "atomic_write_bytes_in_directory",
            side_effect=fail_before_main,
        )
        expected_exception = OSError

    with fault_patch, pytest.raises(expected_exception) as raised:
        security_artifact_redaction.apply_remediation(
            root,
            findings,
            tmp_path / "quarantine",
            tmp_path / "reports",
        )

    if isinstance(raised.value, release_identity.ReleasePublicationCommittedError):
        assert release_identity.publication_outcome(raised.value)["recovery_action"] == (
            "original_family_restored"
        )
    assert (root / "history.sqlite-wal").is_file()
    assert (root / "history.sqlite-shm").is_file()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value, note FROM events").fetchone() == (
            "https://example.test/?access_token=REAL_SECRET_VALUE",
            "keep-me",
        )
    finally:
        connection.close()
    assert not [
        path
        for path in root.iterdir()
        if "sqlite-stage" in path.name
        or path.name.endswith(".retired")
        or path.name.endswith(".sqlite-transition.json")
    ]


def test_release_cli_reports_committed_recovery_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = release_identity.ReleasePublicationCommittedError("injected")
    error.publication_details = {
        "recovery_action": "redacted_family_finalized"
    }

    def fail() -> int:
        raise error

    assert release_identity.run_release_cli(fail, component="r23-test") == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "committed_durability_uncertain"
    assert payload["committed"] is True
    assert payload["recovery_action"] == "redacted_family_finalized"


def test_registered_directory_producers_bind_snapshot_to_seal_and_avoid_path_rename() -> None:
    project = Path(__file__).resolve().parents[1]
    for relative in (
        "src/geospoiler/operations/build_release_evidence.py",
        "src/geospoiler/operations/build_release_verification.py",
    ):
        source = (project / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert not [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "replace"
        ]
        seal_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "seal"
        ]
        assert seal_calls
        assert all(len(node.args) == 1 and not node.keywords for node in seal_calls)
        assert "capture_release_artifact_tree_snapshot(" in source
        assert "analyzed_snapshot=analyzed_snapshot" in source
