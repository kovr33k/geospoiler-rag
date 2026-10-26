from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import (
    build_release_evidence,
    build_release_verification,
    release_identity,
    security_artifact_redaction,
    security_release_scan,
)


def _make_junction(path: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("junction creation failed")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_common_directory_publication_blocks_parent_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "publication"
    parent.mkdir()
    temporary = parent / ".attempt.tmp"
    temporary.mkdir()
    (temporary / "validated.txt").write_text("validated", encoding="utf-8")
    target = parent / "attempt-001"
    stranded = tmp_path / "stranded"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".attempt.tmp").mkdir()
    (outside / ".attempt.tmp" / "attacker.txt").write_text("attacker", encoding="utf-8")
    original = release_identity._windows_rename_open_directory
    swap_attempted = False
    parent_swap_succeeded = False
    source_swap_succeeded = False

    def swap_then_replace(handle: int, destination: Path) -> None:
        nonlocal swap_attempted, parent_swap_succeeded, source_swap_succeeded
        swap_attempted = True
        try:
            os.replace(parent, stranded)
        except OSError:
            pass
        else:
            parent_swap_succeeded = True
            _make_junction(parent, outside)
        try:
            os.replace(temporary, parent / "replacement")
        except OSError:
            pass
        else:
            source_swap_succeeded = True
        original(handle, destination)

    monkeypatch.setattr(
        release_identity, "_windows_rename_open_directory", swap_then_replace
    )
    try:
        release_identity.durable_replace(temporary, target)
        assert swap_attempted
        assert not parent_swap_succeeded
        assert not source_swap_succeeded
        assert (target / "validated.txt").read_text(encoding="utf-8") == "validated"
        assert not (outside / "attempt-001").exists()
    finally:
        if parent.exists() and release_identity._path_is_link_or_reparse(parent):
            os.rmdir(parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_common_directory_publication_rejects_existing_junction(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "publication"
    _make_junction(parent, outside)
    (outside / "temporary").mkdir()
    try:
        with pytest.raises(RuntimeError, match="boundary"):
            release_identity.durable_replace(parent / "temporary", parent / "attempt-001")
        assert not (outside / "attempt-001").exists()
    finally:
        os.rmdir(parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows source locking contract")
@pytest.mark.parametrize("file_type", ["text", "sqlite"])
def test_redaction_holds_source_parent_until_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_type: str
) -> None:
    root = tmp_path / "artifacts"
    parent = root / "nested"
    parent.mkdir(parents=True)
    if file_type == "text":
        source = parent / "item.txt"
        source.write_text("access_token=hidden", encoding="utf-8")
        hook_name = "_redact_text_snapshot"
    else:
        source = parent / "item.sqlite"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE records(value TEXT)")
            connection.execute("INSERT INTO records VALUES ('access_token=hidden')")
            connection.commit()
        hook_name = "_redact_sqlite_snapshot"
    finding = security_artifact_redaction.scan(root)[0]
    original = getattr(security_artifact_redaction, hook_name)
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    swap_succeeded = False

    def swap_then_redact(*args: object, **kwargs: object) -> int:
        nonlocal swap_succeeded
        try:
            os.replace(parent, moved)
        except OSError:
            pass
        else:
            swap_succeeded = True
            _make_junction(parent, outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(security_artifact_redaction, hook_name, swap_then_redact)
    try:
        report = security_artifact_redaction.apply_remediation(
            root, [finding], tmp_path / "quarantine", tmp_path / "reports"
        )
        assert report["status"] == "passed"
        assert not swap_succeeded
        assert not list(outside.iterdir())
    finally:
        if parent.exists() and security_artifact_redaction._is_link_or_reparse(parent):
            os.rmdir(parent)


def test_security_scan_digest_and_verdict_use_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.txt"
    secret = b"access_token=live-secret"
    candidate.write_bytes(secret)
    original_scan = security_release_scan._scan_path
    original_digest = security_release_scan._inventory_digest
    observed_digests: list[str] = []

    def mutate_after_snapshot(snapshot: Path | bytes, relative: str) -> tuple[str, int]:
        candidate.write_bytes(b"safe")
        assert snapshot == secret
        return original_scan(snapshot, relative)

    def record_digest(items: object) -> str:
        digest = original_digest(items)  # type: ignore[arg-type]
        observed_digests.append(digest)
        return digest

    monkeypatch.setattr(security_release_scan, "_candidate_files", lambda: [candidate])
    monkeypatch.setattr(security_release_scan, "_git_file_names", lambda *_args: set())
    monkeypatch.setattr(security_release_scan, "_scan_path", mutate_after_snapshot)
    monkeypatch.setattr(security_release_scan, "_inventory_digest", record_digest)
    monkeypatch.setattr(security_release_scan.config, "PROJECT_ROOT", tmp_path)
    report = security_release_scan.scan()
    candidate.write_bytes(secret)

    assert report["status"] == "failed"
    assert report["finding_files"] == 1
    assert report["inventory_sha256"] == observed_digests[0]
    assert observed_digests[0] != observed_digests[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_artifact_tree_scan_rejects_junction_candidate(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "safe.txt").write_text("safe", encoding="utf-8")
    link = root / "linked"
    _make_junction(link, outside)
    try:
        report = release_identity.scan_release_artifact_tree(root)
        assert report["status"] == "failed"
        assert report["errors"]
    finally:
        os.rmdir(link)


def test_kernel_lock_file_is_persistent_and_released(tmp_path: Path) -> None:
    lock = tmp_path / ".attempt-001.lock"
    payload = {
        "schema_version": "release_bundle_lock_v2",
        "pid": os.getpid(),
        "hostname": "test",
        "attempt_name": "attempt-001",
        "target_path": str(tmp_path / "attempt-001"),
        "created_at": "2026-08-26T00:00:00+00:00",
        "lock_token": "fixture-token",
    }
    with build_release_evidence._held_release_bundle_lock(lock, payload):
        assert lock.is_file()
    assert lock.is_file()
    assert json.loads(lock.read_text(encoding="utf-8"))["status"] == "released"


def test_second_process_cannot_acquire_kernel_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".attempt-001.lock"
    code = (
        "import os,sys; from pathlib import Path; "
        "from scripts import build_release_evidence as b; "
        "fd=os.open(Path(sys.argv[1]), os.O_RDWR|os.O_CREAT); state=b._lock_descriptor(fd); "
        "print('ready', flush=True); sys.stdin.readline(); b._unlock_descriptor(fd,state); os.close(fd)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(FileExistsError, match="already active"):
            with build_release_evidence._held_release_bundle_lock(
                lock,
                {
                    "schema_version": "release_bundle_lock_v2",
                    "pid": os.getpid(),
                    "attempt_name": "attempt-001",
                    "target_path": str(tmp_path / "attempt-001"),
                    "created_at": "2026-08-26T00:00:00+00:00",
                    "lock_token": "second",
                },
            ):
                pytest.fail("second writer entered")
    finally:
        if process.stdin is not None:
            process.stdin.write("\n")
            process.stdin.flush()
        process.wait(timeout=10)


def test_posix_tree_fsyncs_nested_directories_bottom_up(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "item.txt").write_text("data", encoding="utf-8")
    opened: list[Path] = []
    descriptors = iter(range(100, 110))

    def record_open(path: Path, *_args: object, **_kwargs: object) -> int:
        opened.append(Path(path))
        return next(descriptors)

    with (
        patch.object(release_identity, "os", SimpleNamespace(**{**vars(os), "name": "posix"})),
        patch.object(release_identity.os, "open", side_effect=record_open),
        patch.object(release_identity.os, "fsync"),
        patch.object(release_identity.os, "close"),
    ):
        release_identity.durable_prepare_tree(root)
    assert [str(path).replace("\\", "/") for path in opened] == [
        str(path).replace("\\", "/") for path in (nested, nested.parent, root)
    ]


def test_tree_prepare_error_is_not_suppressed(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "item.txt").write_text("data", encoding="utf-8")
    with patch.object(release_identity.os, "fsync", side_effect=OSError("durability failed")):
        with pytest.raises(OSError, match="durability failed"):
            release_identity.durable_prepare_tree(root)


def test_bundle_and_verifier_use_shared_tree_preparation() -> None:
    bundle_source = Path(build_release_evidence.__file__).read_text(encoding="utf-8")
    verifier_source = Path(build_release_verification.__file__).read_text(encoding="utf-8")
    assert "durable_prepare_tree(prepared_dir)" in bundle_source
    assert verifier_source.count("durable_prepare_tree(") >= 3
    assert "prepared_publication_tree(" in bundle_source
    assert verifier_source.count("prepared_publication_tree(") >= 3
    assert "def _fsync_tree" not in verifier_source


def test_failure_artifact_preserves_committed_uncertain_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "attempt-001"
    prepared = tmp_path / ".attempt-001.prepared"
    prepared.mkdir()
    report = {
        "status": "passed",
        "corpus": {"normalized_txt": 0, "enriched_cards": 0, "media": {"total": 0, "card_sidecar_mismatches": 0}},
        "graph": {"processed": 0},
        "manifest": {"sources": 0, "load_report": {}},
        "security": {"release_scan": {}},
        "checks": {"late_fusion_smoke": True},
        "failed_checks": [],
    }
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_release_verification.py", "--input-dir", str(tmp_path / "input"), "--run-dir", str(output), "--release-run-id", "run-1"],
    )
    calls = 0

    original_commit = release_identity.PreparedPublicationTree.commit

    def publish_then_fail(publication: release_identity.PreparedPublicationTree) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("primary precommit failure")
        original_commit(publication)
        raise release_identity.ReleasePublicationCommittedError("secondary uncertain")

    with (
        patch.object(build_release_verification, "build_report", return_value={}),
        patch.object(build_release_verification, "_prepare_verification_publication", return_value=(report, prepared)),
        patch.object(
            release_identity.PreparedPublicationTree,
            "commit",
            autospec=True,
            side_effect=publish_then_fail,
        ),
    ):
        assert build_release_verification.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_artifact_publication"]["status"] == "committed_durability_uncertain"
    assert payload["failure_artifact_publication"]["committed"] is True
    assert Path(payload["failure_artifact_publication"]["path"]).is_dir()


def test_registered_producers_do_not_publish_with_direct_replace() -> None:
    producers = (
        build_release_evidence,
        build_release_verification,
        security_artifact_redaction,
        security_release_scan,
    )
    for module in producers:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "os.replace(" not in source
        assert ".replace(path" not in source
    sync_source = (Path(__file__).resolve().parents[1] / "src/geospoiler/operations/sync_media_contract.py").read_text(encoding="utf-8")
    assert ".media-contract.tmp" not in sync_source
    assert "atomic_write_bytes(" in sync_source
