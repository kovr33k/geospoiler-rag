from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from loader.manifest_contract import load_manifest_file
from scripts import (
    build_release_test_evidence,
    cold_graph_check,
    graph_provenance_reconcile,
    late_fusion_activation_smoke,
    migrate_portable_paths,
    security_release_scan,
    validate_backup_restore,
)
from scripts.release_identity import stamp_report, validate_manifest_run_id


def _secret(letter: str) -> str:
    return "sk-or-v1-" + letter * 24


def test_matching_secret_shaped_run_id_is_rejected_without_leak() -> None:
    token = _secret("R")

    with pytest.raises(ValueError) as caught:
        validate_manifest_run_id(token, {"run_id": token})

    assert token not in str(caught.value)
    assert "release_secret_material_detected" in str(caught.value)


def test_stamp_report_rejects_secret_command_before_hashing() -> None:
    token = _secret("C")
    identity = {"runtime_identity": {}, "manifest_sha256": "a" * 64}

    with pytest.raises(ValueError) as caught:
        stamp_report(
            {"errors": []},
            identity,
            f"python producer.py --output {token}",
            producer="tests.synthetic",
            producer_schema_version="synthetic_v1",
            source_inputs={"manifest_sha256": "a" * 64},
            command_argv=["python", "producer.py", "--output", token],
        )

    assert token not in str(caught.value)
    assert "release_secret_material_detected" in str(caught.value)


def test_manifest_read_error_does_not_publish_secret_path(tmp_path: Path) -> None:
    token = _secret("M")
    validation = load_manifest_file(tmp_path / f"{token}.json")

    serialized = json.dumps(validation.errors)
    assert token not in serialized
    assert len(validation.errors) == 1
    diagnostic = validation.errors[0].split(":")
    assert diagnostic[:3] == [
        "manifest_unreadable",
        "manifest_contract",
        "FileNotFoundError",
    ]
    assert len(diagnostic[3]) == 64
    int(diagnostic[3], 16)


def test_manifest_unknown_secret_field_is_redacted_in_validation_errors(
    tmp_path: Path,
) -> None:
    token = _secret("F")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "accepted_manifest_v2",
                "run_id": "release-1",
                "pipeline_identity": {},
                "sources": [],
                token: True,
            }
        ),
        encoding="utf-8",
    )

    validation = load_manifest_file(manifest)

    assert token not in json.dumps(validation.errors)
    assert any("[REDACTED]" in error for error in validation.errors)


def test_reparse_candidate_is_rejected_before_read(tmp_path: Path) -> None:
    fixture = tmp_path / "linked" / "clean.txt"
    fixture.parent.mkdir()
    fixture.write_text("clean", encoding="utf-8")

    with patch.object(
        security_release_scan,
        "_is_link_or_reparse",
        side_effect=lambda path: path == fixture.parent,
    ):
        candidate = security_release_scan._make_candidate(fixture, "project", tmp_path)

    assert candidate.path is None
    assert candidate.error_code == "candidate_root_escape"


def test_smoke_identity_preflight_happens_before_rag_or_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    manifest = tmp_path / "accepted.json"
    manifest.write_text("{}", encoding="utf-8")
    create_rag = AsyncMock()

    with (
        patch.object(late_fusion_activation_smoke.config, "LATE_FUSION_ENABLED", True),
        patch.object(late_fusion_activation_smoke.config, "RERANKER_ENABLED", False),
        patch.object(late_fusion_activation_smoke, "create_rag", new=create_rag),
        patch.object(
            late_fusion_activation_smoke,
            "preflight_release_identity",
            side_effect=ValueError("manifest_run_id_mismatch"),
        ),
        pytest.raises(ValueError, match="manifest_run_id_mismatch"),
    ):
        asyncio.run(
            late_fusion_activation_smoke.run_smoke(
                output,
                manifest_path=manifest,
                release_run_id="release-1",
            )
        )

    create_rag.assert_not_awaited()
    assert not output.exists()
    assert not (tmp_path / "smoke_attempts.json").exists()


def test_external_configured_output_is_scanned_without_project_relative_crash(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    output = tmp_path / "external-output"
    project.mkdir()
    output.mkdir()
    (output / "clean.txt").write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", project),
        patch.object(security_release_scan.config, "OUTPUT_DIR", output),
        patch.object(
            security_release_scan.config,
            "SOURCE_REGISTRY_DB_PATH",
            project / "source_registry.sqlite",
        ),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "passed"
    assert report["scanned_files"] == 1
    assert report["errors"] == []
    assert report["inventory_sha256"]


def test_unknown_external_candidate_yields_safe_durable_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / ("outside-" + _secret("O")) / "clean.txt"
    project.mkdir()
    external.parent.mkdir()
    external.write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", project),
        patch.object(security_release_scan.config, "OUTPUT_DIR", project / "output"),
        patch.object(
            security_release_scan.config,
            "SOURCE_REGISTRY_DB_PATH",
            project / "source_registry.sqlite",
        ),
        patch.object(security_release_scan, "_candidate_files", return_value=[external]),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    serialized = json.dumps(report)
    assert report["status"] == "failed"
    assert report["unreadable_files"] == 1
    assert report["errors"][0]["error_code"] == "candidate_root_unknown"
    assert _secret("O") not in serialized


def test_tracked_protected_candidate_keeps_candidate_occurrence(tmp_path: Path) -> None:
    token = _secret("P")
    relative = f"private/{token}/.env"
    fixture = tmp_path / relative
    fixture.parent.mkdir(parents=True)
    fixture.write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(security_release_scan, "_candidate_files", return_value=[fixture]),
        patch.object(
            security_release_scan,
            "_git_file_names",
            side_effect=[{relative}, {relative}],
        ),
    ):
        report = security_release_scan.scan()

    assert report["status"] == "failed"
    assert report["finding_files"] == 1
    assert report["occurrences"] == 1
    assert report["metadata_occurrences"] == 1
    assert report["protected_occurrences"] == 1
    assert report["protected_files"] == [
        {"path": "private/[REDACTED]/.env", "occurrences": 1}
    ]
    assert token not in json.dumps(report)


def test_colliding_redacted_candidate_paths_get_distinct_safe_ids(tmp_path: Path) -> None:
    first_token = _secret("X")
    second_token = _secret("Y")
    first = tmp_path / first_token / ".env"
    second = tmp_path / second_token / ".env"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("clean", encoding="utf-8")
    second.write_text("clean", encoding="utf-8")

    with (
        patch.object(security_release_scan.config, "PROJECT_ROOT", tmp_path),
        patch.object(
            security_release_scan, "_candidate_files", return_value=[first, second]
        ),
        patch.object(security_release_scan, "_git_file_names", side_effect=[set(), set()]),
    ):
        report = security_release_scan.scan()

    assert len(report["findings"]) == 2
    assert {item["path"] for item in report["findings"]} == {"[REDACTED]/.env"}
    assert len({item["path_id"] for item in report["findings"]}) == 2
    assert first_token not in json.dumps(report)
    assert second_token not in json.dumps(report)


@pytest.mark.parametrize(
    ("module", "argv", "body_name"),
    (
        (
            validate_backup_restore,
            [
                "validate_backup_restore.py",
                "--manifest",
                "backup.json",
                "--report",
                "report.json",
                "--release-manifest",
                "accepted.json",
                "--release-run-id",
                "release-1",
            ],
            "validate",
        ),
        (
            migrate_portable_paths,
            [
                "migrate_portable_paths.py",
                "apply",
                "--report",
                "report.json",
                "--manifest",
                "accepted.json",
                "--release-run-id",
                "release-1",
            ],
            "apply",
        ),
        (
            graph_provenance_reconcile,
            [
                "graph_provenance_reconcile.py",
                "--report",
                "report.json",
                "--manifest",
                "accepted.json",
                "--release-run-id",
                "release-1",
            ],
            "reconcile",
        ),
        (
            build_release_test_evidence,
            [
                "build_release_test_evidence.py",
                "--execute",
                "--output",
                "report.json",
                "--manifest",
                "accepted.json",
                "--release-run-id",
                "release-1",
                "--base-ref",
                build_release_test_evidence.TRUSTED_RELEASE_BASE_REF,
            ],
            "run_gate",
        ),
        (
            cold_graph_check,
            [
                "cold_graph_check.py",
                "--workspace",
                "workspace",
                "--manifest",
                "accepted.json",
                "--report",
                "report.json",
                "--release-run-id",
                "release-1",
            ],
            "_check",
        ),
    ),
)
def test_cli_preflight_rejection_precedes_producer_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module,
    argv: list[str],
    body_name: str,
) -> None:
    resolved_argv = [str(tmp_path / item) if item.endswith(".json") else item for item in argv]
    body = Mock()
    monkeypatch.setattr("sys.argv", resolved_argv)

    with (
        patch.object(module, body_name, body),
        patch.object(
            module,
            "preflight_release_identity",
            side_effect=ValueError("manifest_run_id_mismatch"),
        ),
        pytest.raises(ValueError, match="manifest_run_id_mismatch"),
    ):
        module.main()

    body.assert_not_called()
    assert not (tmp_path / "report.json").exists()
