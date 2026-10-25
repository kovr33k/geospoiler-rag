import asyncio
import copy
import hashlib
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import config
import llm_backend
from loader import ingest as ingest_module
from loader.manifest_contract import load_manifest_file, validate_manifest_payload
from scripts import (
    build_release_evidence as release_bundle,
)
from scripts import (
    build_release_test_evidence as release_test_evidence,
)
from scripts import (
    build_release_verification,
    cold_graph_check,
    release_identity,
    security_release_scan,
)
from scripts.build_release_evidence import build_bundle
from scripts.build_release_test_evidence import _build_report_from_runner, run_gate
from scripts.build_release_verification import (
    _media_contract_pair,
    _smoke_is_strict,
    _token_rotation_markdown_line,
    _wiki_fts_gate,
)
from scripts.graph_provenance_reconcile import _stable_sqlite_snapshot
from scripts.release_identity import (
    STATIC_ENVIRONMENT_COMMAND_NAMES,
    STATIC_ENVIRONMENT_PACKAGE_NAMES,
    build_identity,
    canonical_json,
    config_identity_sha256,
    corpus_identity_sha256,
    current_static_environment_snapshot,
    dependency_spec_receipt,
    normalise_distribution_name,
    sha256_bytes,
    static_environment_receipt_errors,
    strict_json_loads,
)
from scripts.smoke_contract import OFFICIAL_CASES_SHA256, official_cases_payload


def _official_smoke_payload() -> dict:
    cases = []
    for spec in official_cases_payload():
        cases.append(
            {
                **spec,
                "pipeline": "late_fusion",
                "fallback": False,
                "errors": [],
                "citation_count": 1,
                "reference_count": 1,
                "youtube_start_url_count": 0,
                "references": [{"reference_id": "S1", "url": "https://example.test/source"}],
                "citation_ids": ["S1"],
            }
        )
    cases_sha = sha256_bytes(canonical_json(cases))
    attempt = {
        "attempt_index": 1,
        "attempt_id": "attempt-001",
        "started_at": "2026-08-18T00:00:00+00:00",
        "finished_at": "2026-08-18T00:00:01+00:00",
        "previous_attempt_sha256": None,
        "passed": True,
        "case_count": 10,
        "cases_sha256": cases_sha,
        "errors": [],
    }
    attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
    return {
        "cases": cases,
        "cases_sha256": cases_sha,
        "official_cases_sha256": OFFICIAL_CASES_SHA256,
        "attempt_id": "attempt-001",
        "attempts": [attempt],
    }


def _accepted_manifest_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: object = "release-test",
) -> tuple[Path, dict, str]:
    normalized_root = tmp_path / "output" / "normalized"
    enriched_root = tmp_path / "output" / "enriched"
    normalized = normalized_root / "channel" / "1.txt"
    card = enriched_root / "channel" / "1.enriched.json"
    normalized.parent.mkdir(parents=True)
    card.parent.mkdir(parents=True)
    normalized.write_text("source body", encoding="utf-8")
    graph_text = "Entity: source\nRelation: test"
    normalized_value = normalized.relative_to(tmp_path).as_posix()
    card.write_text(
        json.dumps(
            {
                "schema_version": "enriched_v2",
                "graph_text": graph_text,
                "quality_flags": [],
                "provenance": {
                    "source_id": "telegram:test:1",
                    "normalized_path": normalized_value,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "NORMALIZED_DIR", normalized_root)
    monkeypatch.setattr(config, "YOUTUBE_NORMALIZED_DIR", normalized_root)
    monkeypatch.setattr(config, "ENRICHED_DIR", enriched_root)
    payload = {
        "schema_version": "accepted_manifest_v2",
        "run_id": run_id,
        "pipeline_identity": {
            "llm_profile": config.LLM_PROFILE,
            "enrichment_model": llm_backend.active_model_for("enrichment"),
            "media_contract_version": "media-contract-v2",
            "late_fusion_enabled": config.LATE_FUSION_ENABLED,
            "reranker_enabled": config.RERANKER_ENABLED,
            "wiki_enabled": config.WIKI_ENABLED,
            "expected_cards": 1,
            "config_identity_sha256": config_identity_sha256(),
        },
        "sources": [
            {
                "accepted": True,
                "source_id": "telegram:test:1",
                "normalized_path": normalized_value,
                "card_path": card.relative_to(tmp_path).as_posix(),
                "normalized_sha256": sha256_bytes(normalized.read_bytes()),
                "enriched_sha256": sha256_bytes(card.read_bytes()),
                "graph_text_sha256": sha256_bytes(graph_text.encode("utf-8")),
            }
        ],
    }
    manifest = tmp_path / "accepted_manifest_v2.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest, payload, graph_text


def _static_environment_receipt_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, Path]:
    project_root = tmp_path / "environment-project"
    project_root.mkdir()
    dependencies = ",\n".join(
        f'    "{name}>=0"'
        for name in STATIC_ENVIRONMENT_PACKAGE_NAMES
    )
    (project_root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "environment-fixture"\n'
        'version = "0.0.0"\n'
        "dependencies = [\n"
        f"{dependencies}\n"
        "]\n",
        encoding="utf-8",
    )
    (project_root / "requirements.txt").write_bytes(b"fixture==1.0.0\n")
    monkeypatch.setattr(release_identity, "PROJECT_ROOT", project_root)

    environment_snapshot = current_static_environment_snapshot()
    assert environment_snapshot["errors"] == []
    packages = environment_snapshot["packages"]
    command_suffixes = {
        "python": ["--version"],
        "pytest": ["-m", "pytest", "--version"],
        "ruff": ["-m", "ruff", "--version"],
    }
    commands = {
        name: {
            "argv": [sys.executable, *suffix],
            "status": "passed",
            "exit_code": 0,
            "version": environment_snapshot["command_versions"][name],
            "error": None,
        }
        for name, suffix in command_suffixes.items()
    }
    pip_commands = [
        {
            "package": name,
            "argv": [sys.executable, "-m", "pip", "show", name],
            "status": "passed",
            "exit_code": 0,
            "observed_name": normalise_distribution_name(name),
            "observed_version": packages[name],
            "error": None,
        }
        for name in STATIC_ENVIRONMENT_PACKAGE_NAMES
    ]
    dependency = dependency_spec_receipt(project_root)
    receipt = {
        "status": "passed",
        "commands": commands,
        "pip_show": {
            "status": "passed",
            "packages": packages,
            "missing_packages": [],
            "version_errors": [],
            "commands": pip_commands,
            "error": None,
        },
        "dependency_files": dependency["files"],
        "dependency_spec_identity_sha256": dependency["identity_sha256"],
        "errors": [],
    }
    assert static_environment_receipt_errors(receipt) == []
    return receipt, project_root


def test_manifest_rejects_unknown_and_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads('{"schema_version":"accepted_manifest_v2","schema_version":"accepted_manifest_v2"}')
    result = validate_manifest_payload(
        {
            "schema_version": "accepted_manifest_v2",
            "run_id": "r",
            "pipeline_identity": {},
            "sources": [],
            "forged": True,
        },
        require_nonempty=False,
    )
    assert "manifest unknown field: forged" in result.errors


def test_manifest_rejects_non_string_run_id_before_loader_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payload, _graph_text = _accepted_manifest_fixture(
        tmp_path,
        monkeypatch,
        run_id=123,
    )
    validation = load_manifest_file(manifest)
    assert "manifest run_id must be non-empty string" in validation.errors

    loader = AsyncMock()
    with patch.object(ingest_module, "load_texts", new=loader):
        with pytest.raises(RuntimeError, match="run_id must be non-empty string"):
            asyncio.run(ingest_module.load_from_manifest(object(), manifest))
    loader.assert_not_awaited()

    report = json.loads(
        (tmp_path / "artifacts" / "accepted_manifest_load_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "failed"
    assert "release_identity" not in report
    assert payload["run_id"] == 123


@pytest.mark.parametrize(
    "case",
    (
        "duplicate_root",
        "duplicate_entry",
        "unknown_root",
        "unknown_pipeline_identity",
        "unknown_source_entry",
        "incomplete_identity",
        "config_identity_mismatch",
        "path_outside_roots",
    ),
)
def test_all_release_manifest_consumers_reject_the_same_malformed_fixture(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payload, _graph_text = _accepted_manifest_fixture(tmp_path, monkeypatch)
    if case == "duplicate_root":
        raw = json.dumps(payload).replace(
            '"schema_version": "accepted_manifest_v2",',
            '"schema_version": "accepted_manifest_v2", '
            '"schema_version": "accepted_manifest_v2",',
            1,
        )
        manifest.write_text(raw, encoding="utf-8")
    elif case == "duplicate_entry":
        raw = json.dumps(payload).replace(
            '"accepted": true,',
            '"accepted": true, "accepted": true,',
            1,
        )
        manifest.write_text(raw, encoding="utf-8")
    elif case == "unknown_root":
        payload["forged"] = True
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "unknown_pipeline_identity":
        payload["pipeline_identity"]["forged"] = True
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "unknown_source_entry":
        payload["sources"][0]["forged"] = True
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "incomplete_identity":
        payload["pipeline_identity"].pop("enrichment_model")
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "config_identity_mismatch":
        payload["pipeline_identity"]["config_identity_sha256"] = "0" * 64
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "path_outside_roots":
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        payload["sources"][0]["normalized_path"] = outside.relative_to(tmp_path).as_posix()
        payload["sources"][0]["normalized_sha256"] = sha256_bytes(outside.read_bytes())
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    consumers = (
        ingest_module.load_manifest_file,
        cold_graph_check.load_manifest_file,
        security_release_scan.load_manifest_file,
        build_release_verification.load_manifest_file,
    )
    errors = [consumer(manifest).errors for consumer in consumers]
    assert all(result == errors[0] for result in errors)
    assert errors[0]

    with pytest.raises(ValueError, match="accepted manifest validation failed"):
        cold_graph_check._load_cold_manifest(
            manifest,
            require_release_contract=True,
        )

    security_report = tmp_path / "security.json"
    with (
        patch.object(security_release_scan, "scan", return_value={}),
        patch.object(
            sys,
            "argv",
            [
                "security_release_scan.py",
                "--report",
                str(security_report),
                "--manifest",
                str(manifest),
                "--release-run-id",
                "release-test",
            ],
        ),
    ):
        with pytest.raises(ValueError, match="accepted manifest validation failed"):
            security_release_scan.main()
    assert not security_report.exists()

    with (
        patch.object(
            build_release_verification,
            "_evidence_bundle",
            return_value=({}, {"accepted_manifest": manifest}),
        ),
        patch.object(build_release_verification, "PROJECT_ROOT", tmp_path),
        patch.object(config, "RAG_STORAGE_DIR", tmp_path / "rag_storage"),
        patch.object(build_release_verification, "git_status_porcelain", return_value=""),
        patch.object(build_release_verification, "_git_value", return_value="fixture"),
    ):
        verification_report = build_release_verification.build_report(tmp_path)
    assert verification_report["status"] == "failed"
    assert any(
        error.startswith("manifest_contract:")
        for error in verification_report["errors"]
    )

    loader = AsyncMock()
    with patch.object(ingest_module, "load_texts", new=loader):
        with pytest.raises(RuntimeError, match="Accepted load manifest validation failed"):
            asyncio.run(ingest_module.load_from_manifest(object(), manifest))
    loader.assert_not_awaited()
    report = json.loads(
        (tmp_path / "artifacts" / "accepted_manifest_load_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "failed"
    assert "release_identity" not in report


def test_identity_rejects_manifest_bytes_that_do_not_match_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    old_payload = {
        "schema_version": "accepted_manifest_v2",
        "run_id": "release-test",
        "sources": [],
    }
    new_payload = {
        **old_payload,
        "sources": [
            {
                "source_id": "changed",
                "normalized_path": "changed.txt",
                "card_path": "changed.json",
                "normalized_sha256": "0" * 64,
                "enriched_sha256": "1" * 64,
                "graph_text_sha256": "2" * 64,
            }
        ],
    }
    manifest.write_text(json.dumps(new_payload), encoding="utf-8")
    monkeypatch.setattr(release_identity, "git_commit", lambda: "commit")
    monkeypatch.setattr(release_identity, "git_tree_sha256", lambda: "tree")
    monkeypatch.setattr(release_identity, "working_tree_sha256", lambda: "working")

    with pytest.raises(ValueError, match="manifest bytes do not match validated payload"):
        build_identity("release-test", manifest, old_payload)

    assert corpus_identity_sha256(old_payload) != corpus_identity_sha256(new_payload)


def test_identity_rejects_manifest_bytes_changed_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    payload = {
        "schema_version": "accepted_manifest_v2",
        "run_id": "release-test",
        "sources": [],
    }
    manifest.write_bytes(json.dumps(payload).encode("utf-8") + b"\n")
    validated_bytes = manifest.read_bytes()
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(release_identity, "git_commit", lambda: "commit")
    monkeypatch.setattr(release_identity, "git_tree_sha256", lambda: "tree")
    monkeypatch.setattr(release_identity, "working_tree_sha256", lambda: "working")

    with pytest.raises(ValueError, match="manifest bytes changed after validation"):
        build_identity(
            "release-test",
            manifest,
            payload,
            manifest_bytes=validated_bytes,
        )


def test_identity_rejects_bool_number_equivalence_between_bytes_and_payload(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "accepted_manifest_v2",
                "run_id": "release-test",
                "sources": [],
                "sentinel": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest bytes do not match validated payload"):
        build_identity(
            "release-test",
            manifest,
            {
                "schema_version": "accepted_manifest_v2",
                "run_id": "release-test",
                "sources": [],
                "sentinel": True,
            },
        )


def test_accepted_load_report_binds_exact_graph_text_passed_to_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payload, graph_text = _accepted_manifest_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(release_identity, "git_commit", lambda: "commit")
    monkeypatch.setattr(release_identity, "git_tree_sha256", lambda: "tree")
    monkeypatch.setattr(release_identity, "working_tree_sha256", lambda: "working")
    loader = AsyncMock(return_value=1)

    with patch.object(ingest_module, "load_texts", new=loader):
        assert asyncio.run(ingest_module.load_from_manifest(object(), manifest)) == 1

    loader.assert_awaited_once()
    assert loader.await_args.args[1][0][1] == graph_text
    report = json.loads(
        (tmp_path / "artifacts" / "accepted_manifest_load_report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["loaded_sources"] == [
        {
            "source_id": payload["sources"][0]["source_id"],
            "graph_text_sha256": sha256_bytes(graph_text.encode("utf-8")),
        }
    ]


def test_static_environment_receipt_fails_closed_when_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_test_evidence,
        "_run_environment_command",
        lambda argv: {"argv": argv, "status": "failed", "exit_code": 1, "version": None, "error": "command_failed"},
    )
    monkeypatch.setattr(
        release_test_evidence,
        "_pip_show_receipt",
        lambda: {
            "status": "failed",
            "packages": {"ruff": None},
            "missing_packages": ["ruff"],
        },
    )
    monkeypatch.setattr(
        release_test_evidence,
        "_dependency_files_receipt",
        lambda: {"files": [], "identity_sha256": "0" * 64, "errors": []},
    )

    receipt = release_test_evidence._collect_environment_receipt()

    assert receipt["status"] == "failed"
    assert "missing_package:ruff" in receipt["errors"]


def test_static_environment_receipt_rejects_incompatible_declared_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = {
        name: {
            "argv": [name],
            "status": "passed",
            "exit_code": 0,
            "version": "fixture",
            "error": None,
        }
        for name in STATIC_ENVIRONMENT_COMMAND_NAMES
    }
    packages = {name: "9999.0.0" for name in STATIC_ENVIRONMENT_PACKAGE_NAMES}
    packages["lightrag-hku"] = "0.0.0"
    pip_commands = [
        {
            "package": name,
            "argv": ["python", "-m", "pip", "show", name],
            "status": "passed",
            "exit_code": 0,
            "error": None,
        }
        for name in STATIC_ENVIRONMENT_PACKAGE_NAMES
    ]
    dependency_files = release_test_evidence._dependency_files_receipt()
    receipt = {
        "status": "passed",
        "commands": commands,
        "pip_show": {
            "status": "passed",
            "packages": packages,
            "missing_packages": [],
            "commands": pip_commands,
            "error": None,
        },
        "dependency_files": dependency_files["files"],
        "dependency_spec_identity_sha256": dependency_files["identity_sha256"],
        "errors": [],
    }
    assert "environment_package_version_mismatch:lightrag-hku" in (
        static_environment_receipt_errors(receipt)
    )

    monkeypatch.setattr(
        release_test_evidence,
        "_run_environment_command",
        lambda argv: {
            "argv": argv,
            "status": "passed",
            "exit_code": 0,
            "version": "fixture",
            "error": None,
        },
    )
    monkeypatch.setattr(
        release_test_evidence,
        "_pip_show_receipt",
        lambda: receipt["pip_show"],
    )
    generated = release_test_evidence._collect_environment_receipt()
    assert generated["status"] == "failed"
    assert "environment_package_version_mismatch:lightrag-hku" in generated["errors"]


def test_static_environment_receipt_rejects_command_argv_drift() -> None:
    receipt = release_test_evidence._collect_environment_receipt()
    receipt["commands"]["pytest"]["argv"] = ["not-the-command"]

    assert "environment_command_argv_mismatch:pytest" in static_environment_receipt_errors(receipt)


def test_static_environment_receipt_rejects_consistent_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    receipt["pip_show"]["packages"]["telethon"] = "9999.0.0"
    next(
        item for item in receipt["pip_show"]["commands"] if item["package"] == "telethon"
    )["observed_version"] = "9999.0.0"
    for name in STATIC_ENVIRONMENT_COMMAND_NAMES:
        receipt["commands"][name]["version"] = "fixture"

    errors = static_environment_receipt_errors(receipt)

    assert "environment_installed_version_mismatch:telethon" in errors
    assert "environment_pip_show_installed_version_mismatch:telethon" in errors
    assert "environment_command_version_mismatch:python" in errors
    assert "environment_command_version_mismatch:pytest" in errors
    assert "environment_command_version_mismatch:ruff" in errors


def test_static_environment_receipt_rejects_coherent_fake_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    fake_version = "9999.0.0"
    for name in STATIC_ENVIRONMENT_PACKAGE_NAMES:
        receipt["pip_show"]["packages"][name] = fake_version
        next(
            item for item in receipt["pip_show"]["commands"] if item["package"] == name
        )["observed_version"] = fake_version
    receipt["commands"]["python"]["version"] = f"Python {fake_version}"
    receipt["commands"]["pytest"]["version"] = f"pytest {fake_version}"
    receipt["commands"]["ruff"]["version"] = f"ruff {fake_version}"

    errors = static_environment_receipt_errors(receipt)

    assert "environment_installed_version_mismatch:telethon" in errors
    assert "environment_pip_show_installed_version_mismatch:pytest" in errors
    assert "environment_command_version_mismatch:python" in errors
    assert "environment_command_version_mismatch:pytest" in errors
    assert "environment_command_version_mismatch:ruff" in errors


def test_static_environment_receipt_requires_exact_command_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    receipt["commands"]["python"].pop("error")

    assert "environment_command_fields_invalid:python" in static_environment_receipt_errors(receipt)


def test_static_environment_receipt_rejects_non_empty_command_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    receipt["commands"]["ruff"]["error"] = "spoofed"

    assert "environment_command_error_not_empty:ruff" in static_environment_receipt_errors(receipt)


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_static_environment_receipt_rejects_extra_or_missing_command(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    if mutation == "extra":
        receipt["commands"]["extra"] = copy.deepcopy(receipt["commands"]["python"])
    else:
        receipt["commands"].pop("ruff")

    errors = static_environment_receipt_errors(receipt)

    assert "environment_command_set_mismatch" in errors
    if mutation == "missing":
        assert "environment_command_missing:ruff" in errors


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_static_environment_receipt_rejects_extra_or_missing_package(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    if mutation == "extra":
        receipt["pip_show"]["packages"]["extra"] = "1.0.0"
    else:
        receipt["pip_show"]["packages"].pop("telethon")

    assert "environment_package_set_mismatch" in static_environment_receipt_errors(receipt)


def test_static_environment_receipt_fails_closed_when_current_metadata_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    snapshot = current_static_environment_snapshot()
    snapshot["packages"]["telethon"] = None
    snapshot["errors"] = ["environment_installed_package_missing:telethon"]
    monkeypatch.setattr(release_identity, "current_static_environment_snapshot", lambda: snapshot)

    errors = static_environment_receipt_errors(receipt)

    assert "environment_installed_package_missing:telethon" in errors


def test_current_static_environment_snapshot_fails_closed_for_missing_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_version = release_identity.importlib_metadata.version

    def fake_version(name: str) -> str:
        if name == "telethon":
            raise release_identity.importlib_metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(release_identity.importlib_metadata, "version", fake_version)

    snapshot = current_static_environment_snapshot()

    assert snapshot["packages"]["telethon"] is None
    assert "environment_installed_package_missing:telethon" in snapshot["errors"]


def test_producer_environment_receipt_matches_current_metadata() -> None:
    receipt = release_test_evidence._collect_environment_receipt()

    assert receipt["status"] == "passed", (
        receipt["errors"],
        {name: command.get("error") for name, command in receipt["commands"].items()},
    )
    assert static_environment_receipt_errors(receipt) == []


def test_builder_uses_shared_exact_dependency_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"files": [], "identity_sha256": "0" * 64, "errors": []}
    with patch.object(release_test_evidence, "dependency_spec_receipt", return_value=expected) as helper:
        assert release_test_evidence._dependency_files_receipt() == expected
    helper.assert_called_once_with(release_test_evidence.PROJECT_ROOT)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ("sha256", "environment_dependency_files_mismatch"),
        ("bytes", "environment_dependency_files_mismatch"),
        ("aggregate", "environment_dependency_identity_mismatch"),
    ),
)
def test_static_environment_receipt_rejects_forged_dependency_identity(
    mutation: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    if mutation == "sha256":
        receipt["dependency_files"][0]["sha256"] = "0" * 64
    elif mutation == "bytes":
        receipt["dependency_files"][0]["bytes"] += 1
    else:
        receipt["dependency_spec_identity_sha256"] = "0" * 64

    assert expected_error in static_environment_receipt_errors(receipt)


def test_static_environment_receipt_rejects_wrong_path_bytes_framing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    wrong_identity = hashlib.sha256()
    for item in reversed(receipt["dependency_files"]):
        wrong_identity.update(item["path"].encode("utf-8"))
        wrong_identity.update(b"\0")
        wrong_identity.update((project_root / item["path"]).read_bytes())
    receipt["dependency_spec_identity_sha256"] = wrong_identity.hexdigest()

    assert "environment_dependency_identity_mismatch" in static_environment_receipt_errors(receipt)


def test_static_environment_receipt_rejects_dependency_file_changed_after_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    (project_root / "requirements.txt").write_bytes(b"changed==2.0.0\n")

    errors = static_environment_receipt_errors(receipt)
    assert "environment_dependency_files_mismatch" in errors
    assert "environment_dependency_identity_mismatch" in errors


def test_static_environment_receipt_rejects_observed_name_and_version_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    receipt["pip_show"]["commands"][0]["observed_name"] = "wrong-name"
    receipt["pip_show"]["packages"]["lightrag-hku"] = "2.0.0"

    errors = static_environment_receipt_errors(receipt)
    assert "environment_pip_show_observed_name_mismatch:telethon" in errors
    assert "environment_pip_show_observed_version_mismatch:lightrag-hku" in errors


@pytest.mark.parametrize("variant", ("duplicate", "missing", "extra"))
def test_static_environment_receipt_rejects_duplicate_missing_or_extra_package_receipt(
    variant: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, _project_root = _static_environment_receipt_fixture(tmp_path, monkeypatch)
    commands = receipt["pip_show"]["commands"]
    if variant == "duplicate":
        commands[-1] = copy.deepcopy(commands[0])
        expected_error = "environment_pip_show_duplicate_package:telethon"
    elif variant == "missing":
        commands.pop()
        expected_error = "environment_pip_show_commands_invalid"
    else:
        commands[-1]["package"] = "not-installed"
        expected_error = "environment_pip_show_package_failed:not-installed"

    errors = static_environment_receipt_errors(receipt)
    assert expected_error in errors
    if variant != "missing":
        assert "environment_pip_show_package_set_mismatch" in errors


def test_pip_show_receipt_publishes_normalized_observed_name_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        package = argv[-1]
        return SimpleNamespace(
            returncode=0,
            stdout=f"Name: {package.replace('-', '_')}\nVersion: 1.0.0\n",
            stderr="",
        )

    monkeypatch.setattr(release_test_evidence.subprocess, "run", fake_run)
    monkeypatch.setattr(release_test_evidence, "static_environment_package_version_errors", lambda _packages: [])

    receipt = release_test_evidence._pip_show_receipt()

    assert receipt["status"] == "passed"
    assert all(
        item["observed_name"] == normalise_distribution_name(item["package"])
        and item["observed_version"] == "1.0.0"
        and item["error"] is None
        for item in receipt["commands"]
    )


def test_verification_markdown_does_not_claim_unrecorded_token_attestation() -> None:
    failed_line = _token_rotation_markdown_line(
        {"checks": {"token_rotation": False}, "token_rotation": {}}
    )
    passed_line = _token_rotation_markdown_line(
        {
            "checks": {"token_rotation": True},
            "token_rotation": {"user_attestation_recorded": True},
        }
    )

    assert "NOT CLOSED" in failed_line
    assert "closed by explicit user attestation" in passed_line


def test_runner_report_cannot_be_built_from_manual_exit_codes(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"gates_passed": 4, "gates_failed": 0, "timeouts": 0, "commands": []}), encoding="utf-8")
    logs = {name: tmp_path / f"{name}.log" for name in ("pytest", "ruff", "compileall", "diff_check")}
    for path in logs.values():
        path.write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="built by run_gate"):
        _build_report_from_runner(object())


def test_runner_rejects_reused_nonempty_staging(tmp_path: Path) -> None:
    staging = tmp_path / "attempt-001"
    staging.mkdir()
    (staging / "old-receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="new and empty"):
        run_gate(staging, base_ref="HEAD")


def test_bundle_recovers_dead_lock_metadata_under_kernel_lock(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    output = tmp_path / "attempt-001"
    lock = tmp_path / ".attempt-001.lock"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "release_bundle_lock_v1",
                "pid": 999999,
                "hostname": socket.gethostname(),
                "attempt_name": "attempt-001",
                "target_path": str(output),
                "secret": "must-not-enter-recovery-artifact",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="regular file"):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("scripts.build_release_evidence.assert_clean_release_tree", lambda: None)
            build_bundle(output, staging / "manifest.json", "run-1", [], staging_root=staging)
    assert lock.exists()
    assert json.loads(lock.read_text(encoding="utf-8"))["status"] == "released"
    assert not list(tmp_path.glob(".attempt-001.lock.stale-*-*"))
    recovery_receipts = list(tmp_path.glob(".attempt-001.lock.recovery-*-*.json"))
    assert len(recovery_receipts) == 1
    recovery = json.loads(recovery_receipts[0].read_text(encoding="utf-8"))
    assert recovery["status"] == "failed"
    assert recovery["reason"] == "interrupted_kernel_lock_recovered"
    assert "must-not-enter-recovery-artifact" not in recovery_receipts[0].read_text(encoding="utf-8")


def test_bundle_recovers_malformed_metadata_after_kernel_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    output = tmp_path / "attempt-001"
    lock = tmp_path / ".attempt-001.lock"
    lock.write_text("{", encoding="utf-8")

    monkeypatch.setattr(release_bundle, "assert_clean_release_tree", lambda: None)
    with pytest.raises(ValueError, match="regular file"):
        build_bundle(output, staging / "manifest.json", "run-1", [], staging_root=staging)

    assert lock.exists()
    assert json.loads(lock.read_text(encoding="utf-8"))["status"] == "released"
    receipts = list(tmp_path.glob(".attempt-001.lock.recovery-*-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["reason"] == "malformed_lock_metadata_recovered"


def test_bundle_kernel_lock_prevents_replacing_coordination_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    output = tmp_path / "attempt-001"
    lock = tmp_path / ".attempt-001.lock"

    replacement_blocked = False

    def replace_lock(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal replacement_blocked
        try:
            lock.unlink()
        except PermissionError:
            replacement_blocked = True
        return {"status": "ok"}

    monkeypatch.setattr(release_bundle, "_build_bundle", replace_lock)

    assert build_bundle(output, staging / "manifest.json", "run-1", [], staging_root=staging) == {"status": "ok"}
    assert replacement_blocked
    assert json.loads(lock.read_text(encoding="utf-8"))["status"] == "released"


def test_manifest_rejects_unknown_root_field_for_unknown_schema() -> None:
    result = validate_manifest_payload(
        {
            "schema_version": "future_manifest_v9",
            "run_id": "r",
            "sources": [],
            "forged": True,
        },
        require_nonempty=False,
    )
    assert "manifest unknown field: forged" in result.errors


def test_bundle_blocks_live_kernel_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    output = tmp_path / "attempt-001"
    lock = tmp_path / ".attempt-001.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT)
    state = release_bundle._lock_descriptor(descriptor)
    if os.name == "nt":
        monkeypatch.setattr(
            "scripts.build_release_evidence.os.kill",
            lambda *_args: pytest.fail("Windows PID probing must not call os.kill"),
        )
    try:
        with pytest.raises(FileExistsError, match="already active"):
            build_bundle(output, staging / "manifest.json", "run-1", [], staging_root=staging)
    finally:
        release_bundle._unlock_descriptor(descriptor, state)
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def test_smoke_requires_frozen_official_cases() -> None:
    payload = _official_smoke_payload()
    assert _smoke_is_strict(payload)
    payload["cases"][0]["id"] = "EASY_CASE"
    assert not _smoke_is_strict(payload)


def test_sqlite_snapshot_reads_one_backup_and_cleans_temp(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO rows(value) VALUES ('one')")
        connection.commit()
    rows, identity = _stable_sqlite_snapshot(database, {"rows": "SELECT id, value FROM rows ORDER BY id"})
    assert rows["rows"] == [[1, "one"]]
    assert identity["protocol"] == "sqlite_backup_v1"
    assert not list(tmp_path.glob(".state.snapshot-*.sqlite"))


def test_media_contract_rejects_stale_pipeline_owned_diagnostics() -> None:
    meta = SimpleNamespace(
        media=[
            {
                "media_type": "video",
                "message_id": 7,
                "download_status": "downloaded",
                "transcription_status": "transcribed",
                "visual_status": "success",
            }
        ]
    )
    card = SimpleNamespace(
        quality_flags=["transcript_unavailable", "user_quality_note"],
        extraction_issues=["media:video:7:transcription_failed", "user:note"],
    )
    mismatch = _media_contract_pair(meta, card, "fixture.meta.json")
    assert mismatch is not None
    assert mismatch["stale_flags"] == ["transcript_unavailable"]
    assert mismatch["stale_issues"] == ["media:video:7:transcription_failed"]


def test_wiki_fts_gate_is_conditional_and_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WIKI_ENABLED", False)
    assert _wiki_fts_gate({"wiki_fts": 0, "wiki_documents_fts": 0})
    assert not _wiki_fts_gate({"wiki_fts": 1, "wiki_documents_fts": 0})

    monkeypatch.setattr(config, "WIKI_ENABLED", True)
    assert _wiki_fts_gate({"wiki_fts": 2, "wiki_documents_fts": 2})
    assert not _wiki_fts_gate({"wiki_fts": 2, "wiki_documents_fts": 1})
    assert not _wiki_fts_gate({"wiki_fts": 0, "wiki_documents_fts": 0})
