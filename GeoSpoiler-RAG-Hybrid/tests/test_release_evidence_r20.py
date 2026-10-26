from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from loader import manifest_contract
from scripts import (
    build_release_evidence,
    build_release_verification,
    cold_graph_check,
    release_identity,
    security_artifact_redaction,
)
from tests.test_release_evidence_contract import _release_fixture_reports, _valid_manifest


def _subprocess_run_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]


def test_every_registered_release_subprocess_detaches_stdin() -> None:
    for spec in release_identity.RELEASE_PRODUCER_REGISTRY:
        path = Path(spec.path)
        for call in _subprocess_run_calls(path):
            stdin = next((item.value for item in call.keywords if item.arg == "stdin"), None)
            assert isinstance(stdin, ast.Attribute), f"missing stdin=DEVNULL: {path}:{call.lineno}"
            assert isinstance(stdin.value, ast.Name) and stdin.value.id == "subprocess"
            assert stdin.attr == "DEVNULL", f"stdin is not DEVNULL: {path}:{call.lineno}"


def test_cold_graph_report_uses_shared_publication_state_machine(tmp_path: Path) -> None:
    output = tmp_path / "cold.json"
    payload = {"schema_version": "cold_graph_extraction_v1", "status": "failed"}
    with patch.object(cold_graph_check, "atomic_write_release_json") as writer:
        cold_graph_check._atomic_write_report(output, payload)
    writer.assert_called_once_with(output, payload, label="cold_graph_report")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_quarantine_parent_swap_cannot_write_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    source = artifacts / "nested" / "item.txt"
    source.parent.mkdir(parents=True)
    source.write_text("access_token=hidden", encoding="utf-8")
    quarantine = tmp_path / "quarantine"
    safe_parent = quarantine / "nested"
    safe_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    reports = tmp_path / "reports"
    finding = security_artifact_redaction.scan(artifacts)[0]
    original_copy = security_artifact_redaction._copy_bytes_to_quarantine
    swap_attempted = False
    swap_succeeded = False

    def swap_then_copy(
        source_bytes: bytes,
        target: Path,
        expected_sha: str,
        *,
        allow_existing: bool = False,
    ) -> None:
        nonlocal swap_attempted, swap_succeeded
        swap_attempted = True
        try:
            os.rmdir(safe_parent)
        except OSError:
            pass
        else:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(safe_parent), str(outside)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("junction creation failed")
            swap_succeeded = True
        original_copy(
            source_bytes,
            target,
            expected_sha,
            allow_existing=allow_existing,
        )

    monkeypatch.setattr(
        security_artifact_redaction, "_copy_bytes_to_quarantine", swap_then_copy
    )
    try:
        report = security_artifact_redaction.apply_remediation(
            artifacts, [finding], quarantine, reports
        )
        assert report["status"] == "passed"
        assert swap_attempted
        assert not swap_succeeded
        assert not (outside / "item.txt").exists()
        assert "hidden" not in source.read_text(encoding="utf-8")
    finally:
        if safe_parent.exists() and security_artifact_redaction._is_link_or_reparse(
            safe_parent
        ):
            os.rmdir(safe_parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_report_root_swap_is_locked_until_write_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original_write = security_artifact_redaction._write_json
    swap_succeeded = False

    def swap_then_write(path: Path, payload: dict) -> None:
        nonlocal swap_succeeded
        try:
            os.rmdir(report_root)
        except OSError:
            pass
        else:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(report_root), str(outside)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("junction creation failed")
            swap_succeeded = True
        original_write(path, payload)

    monkeypatch.setattr(security_artifact_redaction, "_write_json", swap_then_write)
    try:
        security_artifact_redaction._write_destination_json(
            report_root,
            "report.json",
            {"schema_version": "security_artifact_inventory_v1", "status": "passed"},
        )
        assert not swap_succeeded
        assert (report_root / "report.json").is_file()
        assert not (outside / "report.json").exists()
    finally:
        if report_root.exists() and security_artifact_redaction._is_link_or_reparse(
            report_root
        ):
            os.rmdir(report_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows destination locking contract")
def test_existing_report_can_be_atomically_replaced_under_parent_lock(tmp_path: Path) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    security_artifact_redaction._write_destination_json(
        report_root,
        "report.json",
        {"schema_version": "security_artifact_inventory_v1", "value": 1},
    )

    security_artifact_redaction._write_destination_json(
        report_root,
        "report.json",
        {"schema_version": "security_artifact_inventory_v1", "value": 2},
    )

    payload = json.loads((report_root / "report.json").read_text(encoding="utf-8"))
    assert payload["value"] == 2


def test_new_quarantine_target_cannot_be_reclassified_after_validation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "quarantine.txt"
    source.write_bytes(b"original")
    target.write_bytes(b"original")

    with pytest.raises(FileExistsError):
        security_artifact_redaction._copy_to_quarantine(
            source,
            target,
            release_identity.sha256_file(source),
            allow_existing=False,
        )


def test_windows_durable_replace_requests_write_through(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new")
    with (
        patch.object(release_identity, "os", SimpleNamespace(**{**vars(os), "name": "nt"})),
        patch.object(release_identity, "pinned_directory", return_value=nullcontext(release_identity.PinnedDirectory(tmp_path, tmp_path))),
        patch.object(release_identity, "_windows_durable_replace") as replace,
    ):
        release_identity.durable_replace(source, target)
    replace.assert_called_once_with(source, target)


def test_windows_replace_flags_include_replace_and_write_through(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new")
    move = patch("ctypes.WinDLL", create=True)
    with move as win_dll:
        api = win_dll.return_value.MoveFileExW
        api.return_value = 1
        release_identity._windows_durable_replace(source, target)
    flags = api.call_args.args[2]
    assert flags & release_identity._MOVEFILE_REPLACE_EXISTING
    assert flags & release_identity._MOVEFILE_WRITE_THROUGH


def test_durable_replace_preserves_committed_outcome(tmp_path: Path) -> None:
    parent = release_identity.PinnedDirectory(
        lexical_path=tmp_path,
        io_path=tmp_path,
        directory_fd=123,
    )
    with (
        patch.object(release_identity, "os", SimpleNamespace(**{**vars(os), "name": "posix"})),
        patch.object(release_identity.os, "replace"),
        patch.object(release_identity.os, "fsync", side_effect=OSError("parent fsync")),
        pytest.raises(release_identity.ReleasePublicationCommittedError) as outcome,
    ):
        release_identity.durable_replace_in_directory(parent, "source", "target")
    assert outcome.value.committed is True


def test_committed_uncertain_cli_payload_cannot_say_passed() -> None:
    payload = release_identity.publication_outcome(
        release_identity.ReleasePublicationCommittedError("uncertain")
    )
    assert payload == {
        "status": "committed_durability_uncertain",
        "committed": True,
    }
    assert json.dumps(payload).find('"status": "passed"') == -1


def test_bundle_preserves_committed_uncertain_state(tmp_path: Path) -> None:
    staging, manifest = _valid_manifest(tmp_path)
    identity = release_identity.build_identity(
        "release-test", manifest, json.loads(manifest.read_text(encoding="utf-8"))
    )
    reports = _release_fixture_reports(staging, manifest, identity)
    output = tmp_path / "attempt-001"

    original_commit = release_identity.PreparedPublicationTree.commit

    def commit_then_fail(publication: release_identity.PreparedPublicationTree) -> None:
        original_commit(publication)
        raise release_identity.ReleasePublicationCommittedError("uncertain")

    with (
        patch.object(build_release_evidence, "assert_clean_release_tree"),
        patch.object(manifest_contract.config, "NORMALIZED_DIR", staging / "normalized"),
        patch.object(
            manifest_contract.config, "YOUTUBE_NORMALIZED_DIR", staging / "normalized"
        ),
        patch.object(manifest_contract.config, "ENRICHED_DIR", staging / "enriched"),
        patch.object(
            release_identity.PreparedPublicationTree,
            "commit",
            autospec=True,
            side_effect=commit_then_fail,
        ),
        pytest.raises(release_identity.ReleasePublicationCommittedError),
    ):
        build_release_evidence._build_bundle(
            output, manifest, "release-test", reports, staging_root=staging
        )
    receipts = list(tmp_path.glob("attempt-001.committed-uncertain-*.json"))
    assert output.is_dir()
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["committed"] is True


def test_final_verifier_never_prints_passed_after_committed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "attempt-001"
    prepared = tmp_path / ".attempt-001.prepared"
    prepared.mkdir()
    report = {
        "status": "passed",
        "corpus": {
            "normalized_txt": 0,
            "enriched_cards": 0,
            "media": {"total": 0, "card_sidecar_mismatches": 0},
        },
        "graph": {"processed": 0},
        "manifest": {"sources": 0, "load_report": {}},
        "security": {"release_scan": {}},
        "checks": {"late_fusion_smoke": True},
        "failed_checks": [],
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_release_verification.py",
            "--input-dir",
            str(tmp_path / "input"),
            "--run-dir",
            str(output),
            "--release-run-id",
            "run-1",
        ],
    )

    original_commit = release_identity.PreparedPublicationTree.commit

    def commit_then_fail(publication: release_identity.PreparedPublicationTree) -> None:
        original_commit(publication)
        raise release_identity.ReleasePublicationCommittedError("uncertain")

    with (
        patch.object(build_release_verification, "build_report", return_value={}),
        patch.object(
            build_release_verification,
            "_prepare_verification_publication",
            return_value=(report, prepared),
        ),
        patch.object(
            release_identity.PreparedPublicationTree,
            "commit",
            autospec=True,
            side_effect=commit_then_fail,
        ),
    ):
        assert build_release_verification.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "committed_durability_uncertain"
    assert payload["committed"] is True
    assert output.is_dir()
    assert not list(tmp_path.glob("attempt-001.failed-*"))
