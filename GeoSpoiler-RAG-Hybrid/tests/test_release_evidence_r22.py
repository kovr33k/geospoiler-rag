from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import release_identity, security_artifact_redaction, security_release_scan


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


@pytest.mark.skipif(os.name != "nt", reason="Windows source-handle contract")
def test_prepared_publication_pins_source_before_validation(tmp_path: Path) -> None:
    source = tmp_path / ".attempt.tmp"
    source.mkdir()
    (source / "validated.txt").write_text("validated", encoding="utf-8")
    target = tmp_path / "attempt-001"

    with release_identity.prepared_publication_tree(source, target) as publication:
        with pytest.raises(OSError):
            os.replace(source, tmp_path / "stranded")
        assert (publication.io_path / "validated.txt").read_text(encoding="utf-8") == "validated"
        publication.seal()
        publication.commit()

    assert (target / "validated.txt").read_text(encoding="utf-8") == "validated"


def test_prepared_publication_rejects_tree_changed_after_seal(tmp_path: Path) -> None:
    source = tmp_path / ".attempt.tmp"
    source.mkdir()
    (source / "validated.txt").write_text("validated", encoding="utf-8")
    target = tmp_path / "attempt-001"

    with release_identity.prepared_publication_tree(source, target) as publication:
        publication.seal()
        (publication.io_path / "attacker.txt").write_text("attacker", encoding="utf-8")
        with pytest.raises(RuntimeError, match="changed after validation"):
            publication.commit()

    assert not target.exists()


def test_prepared_publication_never_replaces_existing_target(tmp_path: Path) -> None:
    source = tmp_path / ".attempt.tmp"
    source.mkdir()
    target = tmp_path / "attempt-001"
    target.mkdir()

    with pytest.raises(FileExistsError):
        with release_identity.prepared_publication_tree(source, target):
            pytest.fail("existing canonical target entered publication context")

    assert source.is_dir()
    assert target.is_dir()


def test_posix_publication_rejects_source_fd_identity_mismatch(tmp_path: Path) -> None:
    source = tmp_path / ".attempt.tmp"
    source.mkdir()
    target = tmp_path / "attempt-001"
    publication = release_identity.PreparedPublicationTree(
        source=source,
        target=target,
        io_path=source,
        parent=release_identity.PinnedDirectory(tmp_path, tmp_path, directory_fd=321),
        source_identity=(1, 2, 0o040755),
        source_fd=123,
        sealed_inventory_sha256="sealed",
    )
    changed = SimpleNamespace(st_dev=1, st_ino=3, st_mode=0o040755)

    with (
        patch.object(release_identity.os, "name", "posix"),
        patch.object(release_identity, "_artifact_tree_inventory_sha256", return_value="sealed"),
        patch.object(release_identity.os, "stat", return_value=changed),
        patch.object(release_identity, "_posix_rename_noreplace") as rename,
        pytest.raises(RuntimeError, match="source identity changed"),
    ):
        publication.commit()

    rename.assert_not_called()


def test_security_scan_fails_when_clean_candidate_becomes_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("clean", encoding="utf-8")
    original_scan = security_release_scan._scan_path
    calls = 0

    def mutate_after_analysis(data: Path | bytes, relative: str) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        result = original_scan(data, relative)
        if calls == 1:
            candidate.write_text("access_token=REAL_SECRET_VALUE", encoding="utf-8")
        return result

    monkeypatch.setattr(security_release_scan, "_candidate_files", lambda: [candidate])
    monkeypatch.setattr(security_release_scan, "_git_file_names", lambda *_args: set())
    monkeypatch.setattr(security_release_scan, "_scan_path", mutate_after_analysis)
    monkeypatch.setattr(security_release_scan.config, "PROJECT_ROOT", tmp_path)

    report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert any(item.get("error_code") == "candidate_inventory_changed" for item in report["errors"])


def test_security_scan_fails_when_candidate_is_added_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    first.write_text("clean", encoding="utf-8")
    second = tmp_path / "second.txt"
    calls = 0

    def candidates() -> list[Path]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [first]
        second.write_text("clean too", encoding="utf-8")
        return [first, second]

    monkeypatch.setattr(security_release_scan, "_candidate_files", candidates)
    monkeypatch.setattr(security_release_scan, "_git_file_names", lambda *_args: set())
    monkeypatch.setattr(security_release_scan.config, "PROJECT_ROOT", tmp_path)

    report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert any(item.get("error_code") == "candidate_inventory_changed" for item in report["errors"])


def test_artifact_tree_scan_fails_when_file_changes_during_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("clean", encoding="utf-8")
    original_matches = release_identity.release_secret_matches
    mutated = False

    def mutate_after_read(value: object) -> object:
        nonlocal mutated
        result = original_matches(value)
        if not mutated and isinstance(value, bytes):
            mutated = True
            candidate.write_text("access_token=REAL_SECRET_VALUE", encoding="utf-8")
        return result

    monkeypatch.setattr(release_identity, "release_secret_matches", mutate_after_read)
    report = release_identity.scan_release_artifact_tree(tmp_path)

    assert report["status"] == "failed"
    assert {item["error"] for item in report["errors"]} & {
        "artifact_inventory_changed",
        "artifact_freshness_failed",
    }


def test_artifact_tree_secret_metadata_on_empty_directory_fails(tmp_path: Path) -> None:
    (tmp_path / "access_token=REAL_SECRET_VALUE").mkdir()

    report = release_identity.scan_release_artifact_tree(tmp_path)

    assert report["status"] == "failed"
    assert report["metadata_occurrences"] > 0
    assert report["occurrences"] > 0
    assert report["findings"][0]["node_type"] == "directory"
    assert "REAL_SECRET_VALUE" not in str(report)


def test_sqlite_wal_family_is_quarantined_and_redacted_without_data_loss(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = root / "history.sqlite"
    _wal_database(database)
    findings = security_artifact_redaction.scan(root)

    report = security_artifact_redaction.apply_remediation(
        root,
        findings,
        tmp_path / "quarantine",
        tmp_path / "reports",
    )

    assert report["status"] == "passed"
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT value, note FROM events").fetchone()
        assert row == ("https://example.test/?access_token=%5BREDACTED%5D", "keep-me")
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    quarantine = tmp_path / "quarantine" / "history.sqlite"
    assert (tmp_path / "quarantine" / "history.sqlite-wal").is_file()
    assert (tmp_path / "quarantine" / "history.sqlite-shm").is_file()
    with sqlite3.connect(quarantine) as connection:
        assert connection.execute("SELECT value, note FROM events").fetchone() == (
            "https://example.test/?access_token=REAL_SECRET_VALUE",
            "keep-me",
        )
    assert not [path for path in root.iterdir() if "sqlite-stage" in path.name or path.name.endswith(".retired")]


def test_sqlite_wal_failure_restores_family_and_removes_private_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = root / "history.sqlite"
    _wal_database(database)
    findings = security_artifact_redaction.scan(root)

    with patch.object(
        security_artifact_redaction,
        "atomic_write_bytes_in_directory",
        side_effect=OSError("injected publication failure"),
    ):
        with pytest.raises(OSError, match="injected publication failure"):
            security_artifact_redaction.apply_remediation(
                root,
                findings,
                tmp_path / "quarantine",
                tmp_path / "reports",
            )

    assert database.is_file()
    assert (root / "history.sqlite-wal").is_file()
    assert (root / "history.sqlite-shm").is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value, note FROM events").fetchone() == (
            "https://example.test/?access_token=REAL_SECRET_VALUE",
            "keep-me",
        )
    assert not [path for path in root.iterdir() if "sqlite-stage" in path.name or path.name.endswith(".retired")]


def test_sqlite_transition_recovery_restores_sidecars_before_main_publication(
    tmp_path: Path,
) -> None:
    main = tmp_path / "history.sqlite"
    main.write_bytes(b"original-main")
    retired_name = ".history.sqlite-wal.crash.retired"
    (tmp_path / retired_name).write_bytes(b"original-wal")
    marker = tmp_path / ".history.sqlite.crash.sqlite-transition.json"
    marker.write_bytes(
        release_identity.canonical_json(
            {
                "schema_version": "sqlite_redaction_transition_v1",
                "main_name": "history.sqlite",
                "redacted_sha256": hashlib.sha256(b"redacted-main").hexdigest(),
                "retired": {"history.sqlite-wal": retired_name},
            }
        )
        + b"\n"
    )

    with release_identity.pinned_directory(tmp_path) as parent:
        family = security_artifact_redaction._sqlite_family_bytes(parent, main.name)

    assert family["history.sqlite"] == b"original-main"
    assert family["history.sqlite-wal"] == b"original-wal"
    assert not marker.exists()
    assert not (tmp_path / retired_name).exists()


def test_sqlite_transition_recovery_finishes_cleanup_after_main_publication(
    tmp_path: Path,
) -> None:
    redacted = b"redacted-main"
    main = tmp_path / "history.sqlite"
    main.write_bytes(redacted)
    retired_name = ".history.sqlite-wal.crash.retired"
    (tmp_path / retired_name).write_bytes(b"original-wal")
    marker = tmp_path / ".history.sqlite.crash.sqlite-transition.json"
    marker.write_bytes(
        release_identity.canonical_json(
            {
                "schema_version": "sqlite_redaction_transition_v1",
                "main_name": "history.sqlite",
                "redacted_sha256": hashlib.sha256(redacted).hexdigest(),
                "retired": {"history.sqlite-wal": retired_name},
            }
        )
        + b"\n"
    )

    with release_identity.pinned_directory(tmp_path) as parent:
        family = security_artifact_redaction._sqlite_family_bytes(parent, main.name)

    assert family == {"history.sqlite": redacted}
    assert not marker.exists()
    assert not (tmp_path / retired_name).exists()


def test_registered_directory_producers_use_prepared_publication() -> None:
    project = Path(__file__).resolve().parents[1]
    bundle = (project / "src/geospoiler/operations/build_release_evidence.py").read_text(encoding="utf-8")
    verifier = (project / "src/geospoiler/operations/build_release_verification.py").read_text(encoding="utf-8")

    assert "with prepared_publication_tree(" in bundle
    assert verifier.count("with prepared_publication_tree(") >= 3
    assert "publication.seal(" in bundle
    assert verifier.count("publication.seal(") >= 3
