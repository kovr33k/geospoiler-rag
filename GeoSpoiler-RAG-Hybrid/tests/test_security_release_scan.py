import json
import sys
from pathlib import Path
from unittest.mock import patch

from scripts import security_release_scan
from scripts.build_release_verification import _security_inventory_is_fresh


def test_release_scan_allows_only_exact_synthetic_fixture_value(tmp_path: Path) -> None:
    fixture = tmp_path / "tests" / "test_security_artifact_redaction.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("https://example.test/?" + "access" + "Token=secret", encoding="utf-8")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[fixture]
    ):
        assert security_release_scan.scan()["finding_files"] == 0

    fixture.write_text("https://example.test/?" + "access" + "Token=real-value", encoding="utf-8")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[fixture]
    ):
        report = security_release_scan.scan()
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1


def test_release_scan_counts_binary_files_without_silent_skip(tmp_path: Path) -> None:
    binary = tmp_path / "sample.bin"
    binary.write_bytes(b"\x00\xff\x01\x02")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[binary]
    ):
        report = security_release_scan.scan()
    assert report["scanned_files"] == 1
    assert report["binary_files"] == 1
    assert report["unreadable_files"] == 0
    assert report["finding_files"] == 0


def test_release_scan_includes_sqlite_sidecar_with_token(tmp_path: Path) -> None:
    wal = tmp_path / "graph.sqlite-wal"
    wal.write_bytes(b"sqlite sidecar access" + b"Token=REAL_SECRET_VALUE")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[wal]
    ):
        report = security_release_scan.scan()
    assert report["candidate_files"] == 1
    assert report["scanned_files"] == 1
    assert report["skipped_files"] == []
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1


def test_release_scan_reports_unreadable_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "unreadable.txt"
    candidate.write_text("placeholder", encoding="utf-8")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[candidate]
    ), patch.object(
        security_release_scan, "_scan_path", side_effect=OSError("denied")
    ):
        report = security_release_scan.scan()
    assert report["unreadable_files"] == 1
    assert report["skipped_files"] == []


def test_release_scan_inventory_changes_when_candidate_changes(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("clean", encoding="utf-8")
    with patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path), patch.object(
        security_release_scan, "_candidate_files", return_value=[candidate]
    ):
        first = security_release_scan.scan()
        candidate.write_text("changed", encoding="utf-8")
        second = security_release_scan.scan()
    assert first["inventory_sha256"] != second["inventory_sha256"]


def test_release_scan_fails_closed_for_protected_env_token(tmp_path: Path) -> None:
    token = "sk-or-v1-" + ("x" * 24)
    fixture = tmp_path / ".env"
    fixture.write_text("OPENROUTER_API_KEY=" + token + "\n", encoding="utf-8")
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["protected_occurrences"] == 1
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1
    assert token not in json.dumps(report)


def test_release_scan_records_clean_protected_env_without_allowlisting_it(tmp_path: Path) -> None:
    fixture = tmp_path / ".env"
    fixture.write_text("\n", encoding="utf-8")
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "passed"
    assert report["protected_occurrences"] == 0
    assert report["protected_files"] == [{"path": ".env", "occurrences": 0}]


def test_release_scan_cli_returns_nonzero_for_protected_env_token(tmp_path: Path) -> None:
    fixture = tmp_path / ".env"
    fixture.write_text("OPENROUTER_API_KEY=" + ("sk-or-v1-" + ("x" * 24)) + "\n", encoding="utf-8")
    report_path = tmp_path / "security.json"
    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
        patch.object(sys, "argv", ["security_release_scan.py", "--report", str(report_path)]),
    ):
        assert security_release_scan.main() == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_verifier_rejects_protected_occurrences_even_when_general_counts_are_zero() -> None:
    expected_inventory = "a" * 64
    scanner_report = {
        "status": "failed",
        "inventory_sha256": expected_inventory,
        "finding_files": 0,
        "occurrences": 0,
        "protected_occurrences": 1,
        "protected_files": [{"path": ".env", "occurrences": 1}],
        "unreadable_files": 0,
        "tracked_protected_files": [],
        "staged_protected_files": [],
        "git_inspection_errors": [],
    }
    with patch.object(security_release_scan, "scan", return_value=scanner_report):
        assert not _security_inventory_is_fresh({"inventory_sha256": expected_inventory})
