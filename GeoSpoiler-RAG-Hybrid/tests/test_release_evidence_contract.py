import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from loader import manifest_contract
from scripts.build_release_evidence import build_bundle
from scripts.build_release_test_evidence import _RUNNER_TOKEN, _RunnerResult
from scripts.build_release_test_evidence import _build_report_from_runner as build_test_report
from scripts.build_release_verification import _evidence_bundle, _test_evidence, _zero_or_empty
from scripts.release_identity import (
    REPORT_CONTRACTS,
    STATIC_ENVIRONMENT_COMMAND_NAMES,
    STATIC_ENVIRONMENT_PACKAGE_NAMES,
    TRUSTED_RELEASE_BASE_REF,
    build_identity,
    canonical_json,
    config_identity_sha256,
    current_static_environment_snapshot,
    dependency_spec_receipt,
    normalise_distribution_name,
    sha256_bytes,
    stamp_report,
    verify_payload_sha256,
)
from scripts.smoke_contract import OFFICIAL_CASES_SHA256, official_cases_payload


def _valid_manifest(tmp_path: Path) -> tuple[Path, Path]:
    staging = tmp_path / "staging"
    normalized = staging / "normalized"
    enriched = staging / "enriched"
    normalized.mkdir(parents=True)
    enriched.mkdir(parents=True)
    source = normalized / "source.txt"
    source.write_text("fixture source", encoding="utf-8")
    graph_text = "Entity: fixture\nRelation: test"
    card = enriched / "source.enriched.json"
    card.write_text(json.dumps({"schema_version": "enriched_v2", "graph_text": graph_text, "provenance": {"source_id": "fixture:1", "normalized_path": str(source.resolve())}, "quality_flags": []}), encoding="utf-8")
    manifest = staging / "manifest.json"
    import config
    import llm_backend
    manifest.write_text(json.dumps({
        "schema_version": "accepted_manifest_v2", "run_id": "release-test", "pipeline_identity": {
            "llm_profile": config.LLM_PROFILE,
            "enrichment_model": llm_backend.active_model_for("enrichment"),
            "media_contract_version": "media-contract-v2",
            "late_fusion_enabled": config.LATE_FUSION_ENABLED,
            "reranker_enabled": config.RERANKER_ENABLED,
            "wiki_enabled": config.WIKI_ENABLED,
            "expected_cards": 1,
            "config_identity_sha256": config_identity_sha256(),
        },
        "sources": [{"accepted": True, "source_id": "fixture:1", "normalized_path": str(source.resolve()), "card_path": str(card.resolve()),
                     "normalized_sha256": sha256_bytes(source.read_bytes()), "enriched_sha256": sha256_bytes(card.read_bytes()),
                     "graph_text_sha256": sha256_bytes(graph_text.encode("utf-8"))}],
    }), encoding="utf-8")
    return staging, manifest


def _fixture_test_report(tmp_path: Path) -> dict:
    commands = {
        "pytest": [sys.executable, "-m", "pytest", "-q"],
        "ruff": [sys.executable, "-m", "ruff", "check", "."],
        "compileall": [sys.executable, "-m", "compileall", "-q", "."],
        "diff_check": ["git", "diff", "--check", "merge-base..HEAD", "--"],
    }
    logs: dict[str, Path] = {}
    receipts: dict[str, dict] = {}
    for name in commands:
        log_path = tmp_path / f"{name}.log"
        log_path.write_text("1 passed\n" if name == "pytest" else name, encoding="utf-8")
        logs[name] = log_path
        receipts[name] = {
            "command_argv": commands[name],
            "cwd": str(Path(__file__).resolve().parents[1]),
            "exit_code": 0,
            "started_at": "2026-08-18T00:00:00+00:00",
            "finished_at": "2026-08-18T00:00:01+00:00",
            "duration_ms": 1000.0,
            "timeout_seconds": 1800.0,
            "timed_out": False,
            "interrupted": False,
            "log_path": str(log_path.resolve()),
            "log_bytes": log_path.stat().st_size,
            "log_sha256": sha256_bytes(log_path.read_bytes()),
        }
    summary = tmp_path / "summary.json"
    environment_snapshot = current_static_environment_snapshot()
    assert environment_snapshot["errors"] == []
    package_versions = environment_snapshot["packages"]
    dependency = dependency_spec_receipt()
    environment = {
        "status": "passed",
        "commands": {
            name: {
                "status": "passed",
                "exit_code": 0,
                "version": environment_snapshot["command_versions"][name],
                "error": None,
                "argv": {
                    "python": [sys.executable, "--version"],
                    "pytest": [sys.executable, "-m", "pytest", "--version"],
                    "ruff": [sys.executable, "-m", "ruff", "--version"],
                }[name],
            }
            for name in STATIC_ENVIRONMENT_COMMAND_NAMES
        },
        "pip_show": {
            "status": "passed",
            "packages": package_versions,
            "missing_packages": [],
            "commands": [
                {
                    "package": name,
                    "status": "passed",
                    "exit_code": 0,
                    "argv": [sys.executable, "-m", "pip", "show", name],
                    "observed_name": normalise_distribution_name(name),
                    "observed_version": package_versions[name],
                    "error": None,
                }
                for name in STATIC_ENVIRONMENT_PACKAGE_NAMES
            ],
            "version_errors": [],
            "error": None,
        },
        "dependency_files": dependency["files"],
        "dependency_spec_identity_sha256": dependency["identity_sha256"],
        "errors": [],
    }
    summary.write_text(
        json.dumps(
            {
                "gates_passed": 4,
                "gates_failed": 0,
                "timeouts": 0,
                "interrupted": False,
                "interruption_error": None,
                "commands": list(commands),
                "pytest_counts": {"parsed": True, "passed": 1, "failed": 0, "skipped": 0},
                "environment": environment,
            }
        ),
        encoding="utf-8",
    )
    receipts_path = tmp_path / "runner_receipts.json"
    receipts_path.write_text(
        json.dumps({"schema_version": "release_test_runner_receipts_v1", "gates": receipts}),
        encoding="utf-8",
    )
    lifecycle_path = tmp_path / "runner_lifecycle.json"
    lifecycle_path.write_text(
        json.dumps(
            {
                "schema_version": "release_test_runner_lifecycle_v1",
                "status": "passed",
                "summary_path": str(summary.resolve()),
                "receipts_path": str(receipts_path.resolve()),
            }
        ),
        encoding="utf-8",
    )
    return build_test_report(
        _RunnerResult(
            summary_path=summary,
            logs=logs,
            exit_codes={name: 0 for name in commands},
            receipts=receipts,
            receipts_path=receipts_path,
            lifecycle_path=lifecycle_path,
            token=_RUNNER_TOKEN,
        )
    )


def _release_fixture_reports(tmp_path: Path, manifest: Path, identity: dict) -> list[tuple[str, Path]]:
    test_report = _fixture_test_report(tmp_path)
    test_report.update({"base_ref": TRUSTED_RELEASE_BASE_REF, "base_commit": "a", "merge_base": "b", "head_commit": "c", "diff_check_argv": ["git", "diff", "--check", "b..HEAD", "--"]})
    test_report = stamp_report(test_report, identity, "python fixture", producer=REPORT_CONTRACTS["tests"]["producer"], producer_schema_version=REPORT_CONTRACTS["tests"]["schema_version"], source_inputs={"manifest_sha256": identity["manifest_sha256"], "corpus_identity_sha256": identity["corpus_identity_sha256"]}, command_argv=["python", "fixture"])
    reports: list[tuple[str, Path]] = [("tests", tmp_path / "tests.json")]
    reports[0][1].write_text(json.dumps(test_report), encoding="utf-8")
    for kind, contract in REPORT_CONTRACTS.items():
        if kind == "tests":
            continue
        payload = {"schema_version": contract["schema_version"], "errors": []}
        if "status" in contract["allowed_payload"]:
            payload["status"] = "passed"
        defaults = {
            "generated_at": "2026-08-18T00:00:00+00:00",
            "records": 0, "unique_source_ids": 0, "source_ids": [], "doc_ids": [],
            "counts": {}, "passed": True, "items": [], "manifest": str(manifest),
            "root": str(tmp_path), "files_declared": 1, "files_checked": 1,
            "nested_paths": [], "canonical_mapping": [], "mode": "check",
            "project_root": str(tmp_path), "files_scanned": 1, "scanned_files": 1,
            "changed_files": 0, "field_changes": 0, "unresolved": [],
            "candidate_files": 1, "inventory_sha256": "0" * 64, "skipped_files": [],
            "excluded_roots": [], "binary_files": 0, "unreadable_files": 0,
            "protected_files": [], "tracked_protected_files": [], "staged_protected_files": [],
            "git_inspection_errors": [], "finding_files": 0, "occurrences": 0, "findings": [],
            "late_fusion_enabled": True, "reranker_enabled": False, "wiki_enabled": False,
            "candidate_path": str(tmp_path), "manifest_path": str(manifest),
            "manifest_sha256": "0" * 64, "manifest_run_id": "release-test",
            "config_identity_sha256": "0" * 64, "effective_cold_runtime": {},
            "source_proof_before_load": {}, "source_proof_sha256": "0" * 64,
            "loaded_doc_proof": {}, "storage_inventory": {}, "source_storage_copied": False,
            "workspace_inventory": {}, "empty_before": True, "complete": True,
            "resume": False, "batch_size": 1, "batch_timeout_seconds": 1.0,
            "checkpoint_path": str(tmp_path / "checkpoint.json"),
            "accepted_manifest_load_report_path": str(tmp_path / "load.json"),
            "source_results": [], "cold_graph_attempt": {}, "attempt_history_path": None,
            "attempt_id": "attempt-001", "attempts": [],
        }
        for field in contract["required_payload"]:
            if field in payload:
                continue
            expected_type = contract["field_types"].get(field)
            if expected_type == "list":
                value = []
            elif expected_type == "dict":
                value = {}
            elif expected_type == "bool":
                value = False
            elif expected_type == "int":
                value = 0
            elif expected_type == "number":
                value = 0.0
            elif expected_type == "sha256":
                value = "0" * 64
            elif expected_type in {"sha256_or_none", "str_or_none", "bool_or_none", "dict_or_none"}:
                value = None
            else:
                value = defaults.get(field, "fixture")
            payload[field] = value
        if kind == "risk_register":
            payload["risks"] = [{"id": "RISK-TOKEN-ROTATION-001", "status": "closed", "completion_basis": "user_attestation"}]
        if kind == "backup":
            backup_source = tmp_path / "backup_source_manifest.json"
            backup_source.write_text(
                json.dumps(
                    {
                        "schema_version": "backup_source_manifest_v1",
                        "root": str(tmp_path),
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            payload.update(
                {
                    "manifest": str(backup_source),
                    "backup_manifest_sha256": hashlib.sha256(
                        backup_source.read_bytes()
                    ).hexdigest(),
                    "backup_inventory_sha256": hashlib.sha256(canonical_json([])).hexdigest(),
                }
            )
        if kind == "late_fusion_smoke":
            ledger = tmp_path / "smoke_attempts.json"
            attempt = {
                "attempt_index": 1,
                "attempt_id": "attempt-001",
                "started_at": "2026-08-18T00:00:00+00:00",
                "finished_at": "2026-08-18T00:00:01+00:00",
                "passed": True,
                "case_count": 10,
                "errors": [],
                "previous_attempt_sha256": None,
            }
            attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
            cases = [
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
                for spec in official_cases_payload()
            ]
            cases_sha256 = sha256_bytes(canonical_json(cases))
            attempt["cases_sha256"] = cases_sha256
            attempt["attempt_sha256"] = sha256_bytes(canonical_json(attempt))
            payload.update(
                {
                    "passed": True,
                    "cases": cases,
                    "cases_sha256": cases_sha256,
                    "official_cases_sha256": OFFICIAL_CASES_SHA256,
                    "attempt_id": "attempt-001",
                    "attempts": [attempt],
                    "attempt_history_path": str(ledger),
                }
            )
            ledger.write_text(json.dumps([attempt]), encoding="utf-8")
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps(stamp_report(payload, identity, kind, producer=contract["producer"], producer_schema_version=contract["schema_version"], source_inputs={"manifest_sha256": identity["manifest_sha256"], "corpus_identity_sha256": identity["corpus_identity_sha256"]}, command_argv=["python", kind])), encoding="utf-8")
        reports.append((kind, path))
    return reports


def test_release_verifier_accepts_list_shaped_empty_security_fields() -> None:
    assert _zero_or_empty([])
    assert _zero_or_empty({})
    assert _zero_or_empty(0)
    assert not _zero_or_empty(["unreadable.bin"])


def test_release_bundle_requires_exact_report_set(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = staging / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "accepted_manifest_v2",
                "run_id": "release-test",
                "pipeline_identity": {},
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "attempt-001"
    with patch("scripts.build_release_evidence.assert_clean_release_tree"):
        with pytest.raises(ValueError, match="exact"):
            build_bundle(output, manifest, "release-test", [], staging_root=staging)


def test_release_bundle_rejects_tampered_report(tmp_path: Path) -> None:
    staging, manifest = _valid_manifest(tmp_path)
    identity = build_identity("release-test", manifest, json.loads(manifest.read_text(encoding="utf-8")))
    reports = _release_fixture_reports(staging, manifest, identity)
    output = tmp_path / "attempt-001"
    with patch("scripts.build_release_evidence.assert_clean_release_tree"), patch.object(manifest_contract.config, "NORMALIZED_DIR", staging / "normalized"), patch.object(manifest_contract.config, "YOUTUBE_NORMALIZED_DIR", staging / "normalized"), patch.object(manifest_contract.config, "ENRICHED_DIR", staging / "enriched"):
        build_bundle(output, manifest, "release-test", reports, staging_root=staging)
    (output / "tests.json").write_text('{"status":"passed"}\n', encoding="utf-8")
    errors: list[str] = []
    _identity, _paths = _evidence_bundle(output, errors)
    assert any("hash_mismatch" in error for error in errors)


def test_release_bundle_rejects_duplicate_resolved_report_path(tmp_path: Path) -> None:
    staging, manifest = _valid_manifest(tmp_path)
    identity = build_identity("release-test", manifest, json.loads(manifest.read_text(encoding="utf-8")))
    reports = _release_fixture_reports(staging, manifest, identity)
    tests_source = next(path for kind, path in reports if kind == "tests")
    reports = [(kind, tests_source if kind == "risk_register" else path) for kind, path in reports]
    with patch("scripts.build_release_evidence.assert_clean_release_tree"), patch.object(manifest_contract.config, "NORMALIZED_DIR", staging / "normalized"), patch.object(manifest_contract.config, "YOUTUBE_NORMALIZED_DIR", staging / "normalized"), patch.object(manifest_contract.config, "ENRICHED_DIR", staging / "enriched"):
        with pytest.raises(ValueError, match="Duplicate resolved evidence report path"):
            build_bundle(tmp_path / "attempt-001", manifest, "release-test", reports, staging_root=staging)


def test_stamp_report_has_self_verifying_envelope() -> None:
    identity = {"runtime_identity": {"model": "fixture"}, "manifest_sha256": "m" * 64, "corpus_identity_sha256": "c" * 64}
    payload = stamp_report({"schema_version": "fixture_v1", "errors": []}, identity, "fixture", producer="fixture", producer_schema_version="fixture_v1", source_inputs={"manifest_sha256": "m" * 64}, command_argv=["python", "fixture"])
    assert verify_payload_sha256(payload)
    payload["status"] = "changed"
    assert not verify_payload_sha256(payload)


def test_test_evidence_requires_all_hashed_zero_exit_checks(tmp_path: Path) -> None:
    report = _fixture_test_report(tmp_path)
    assert _test_evidence(report)["status"] == "passed"
    report["checks"][0]["exit_code"] = None
    assert _test_evidence(report)["status"] == "failed"
    report = _fixture_test_report(tmp_path)
    report["checks"].append(dict(report["checks"][0]))
    assert _test_evidence(report)["status"] == "failed"


def test_final_test_evidence_rejects_consistently_forged_environment(tmp_path: Path) -> None:
    report = _fixture_test_report(tmp_path)
    report["release_schema_version"] = "release_evidence_v1"
    summary_path = Path(report["summary_path"])
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    environment = summary["environment"]
    environment["pip_show"]["packages"]["telethon"] = "9999.0.0"
    next(
        item for item in environment["pip_show"]["commands"] if item["package"] == "telethon"
    )["observed_version"] = "9999.0.0"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    report["summary"] = summary
    report["summary_sha256"] = sha256_bytes(summary_path.read_bytes())

    result = _test_evidence(report)

    assert result["status"] == "failed"
    assert result["reason"] == "static environment receipt failed or missing"


def test_bundle_rejects_dirty_tree_before_output_creation(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = staging / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "accepted_manifest_v2", "run_id": "dirty-test", "sources": []}),
        encoding="utf-8",
    )
    output = tmp_path / "attempt-001"
    with patch("scripts.build_release_evidence.assert_clean_release_tree", side_effect=RuntimeError("dirty")):
        try:
            build_bundle(output, manifest, "dirty-test", [], staging_root=staging)
        except RuntimeError as exc:
            assert str(exc) == "dirty"
        else:  # pragma: no cover
            raise AssertionError("dirty tree must block bundle creation")
    assert not output.exists()
