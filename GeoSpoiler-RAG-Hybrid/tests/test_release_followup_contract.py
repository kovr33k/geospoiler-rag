import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts import build_release_verification, late_fusion_activation_smoke
from scripts.build_release_evidence import build_bundle
from scripts.build_release_verification import _smoke_is_strict, _test_evidence
from scripts.release_identity import (
    REPORT_CONTRACTS,
    build_identity,
    canonical_json,
    sha256_bytes,
    stamp_report,
    validate_report_envelope,
)
from scripts.smoke_contract import OFFICIAL_CASES_SHA256, official_cases_payload


def _identity(tmp_path: Path) -> tuple[Path, dict]:
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest_path = staging / "manifest.json"
    manifest = {"schema_version": "accepted_manifest_v2", "run_id": "followup", "sources": []}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, build_identity("followup", manifest_path, manifest)


def _stub_manifest_validation(manifest_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        payload=json.loads(manifest_path.read_text(encoding="utf-8")),
        errors=[],
        manifest_bytes=manifest_path.read_bytes(),
    )


def test_report_contract_rejects_forged_producer_schema_and_source_inputs(tmp_path: Path) -> None:
    _manifest, identity = _identity(tmp_path)
    contract = REPORT_CONTRACTS["security"]
    report = stamp_report(
        {"schema_version": contract["schema_version"], "errors": []},
        identity,
        "fixture",
        producer=contract["producer"],
        producer_schema_version=contract["schema_version"],
        source_inputs={"manifest_sha256": identity["manifest_sha256"], "corpus_identity_sha256": identity["corpus_identity_sha256"]},
        command_argv=["python", "fixture"],
    )
    report["producer"] = "forged"
    report["producer_schema_version"] = "wrong"
    report["source_inputs"] = {"manifest_sha256": "0" * 64, "corpus_identity_sha256": "1" * 64}
    report["payload_sha256"] = sha256_bytes(canonical_json({key: value for key, value in report.items() if key != "payload_sha256"}))
    errors = validate_report_envelope(report, kind="security", identity=identity)
    assert {"producer_invalid:security", "producer_schema_invalid:security", "source_inputs_manifest_mismatch"} <= set(errors)


def test_report_envelope_rejects_missing_unknown_and_wrong_type_fields(tmp_path: Path) -> None:
    _manifest, identity = _identity(tmp_path)
    contract = REPORT_CONTRACTS["security"]
    base = stamp_report(
        {"schema_version": contract["schema_version"], "errors": []},
        identity,
        "fixture",
        producer=contract["producer"],
        producer_schema_version=contract["schema_version"],
        source_inputs={"manifest_sha256": identity["manifest_sha256"], "corpus_identity_sha256": identity["corpus_identity_sha256"]},
        command_argv=["python", "fixture"],
    )
    missing = dict(base)
    missing.pop("command_argv")
    assert "envelope_missing:command_argv" in validate_report_envelope(missing, kind="security", identity=identity)

    unknown = dict(base)
    unknown["release_forged_field"] = True
    unknown["payload_sha256"] = sha256_bytes(canonical_json({key: value for key, value in unknown.items() if key != "payload_sha256"}))
    assert "envelope_unknown:release_forged_field" in validate_report_envelope(unknown, kind="security", identity=identity)

    wrong_type = dict(base)
    wrong_type["command_argv"] = "python fixture"
    wrong_type["payload_sha256"] = sha256_bytes(canonical_json({key: value for key, value in wrong_type.items() if key != "payload_sha256"}))
    assert "command_argv_invalid" in validate_report_envelope(wrong_type, kind="security", identity=identity)


def test_bundle_rejects_incomplete_and_reserved_kinds(tmp_path: Path) -> None:
    manifest, identity = _identity(tmp_path)
    report = tmp_path / "tests.json"
    contract = REPORT_CONTRACTS["tests"]
    report.write_text(
        json.dumps(
            stamp_report(
                {"schema_version": contract["schema_version"], "errors": []},
                identity,
                "fixture",
                producer=contract["producer"],
                producer_schema_version=contract["schema_version"],
                source_inputs={"manifest_sha256": identity["manifest_sha256"], "corpus_identity_sha256": identity["corpus_identity_sha256"]},
                command_argv=["python", "fixture"],
            )
        ),
        encoding="utf-8",
    )
    with (
        patch("scripts.build_release_evidence.assert_clean_release_tree"),
        patch(
            "scripts.build_release_evidence.load_manifest_file",
            return_value=_stub_manifest_validation(manifest),
        ),
    ):
        with pytest.raises(ValueError, match="exact"):
            build_bundle(tmp_path / "attempt-001", manifest, "followup", [("tests", report)], staging_root=manifest.parent)
        with pytest.raises(ValueError, match="exact"):
            build_bundle(tmp_path / "attempt-002", manifest, "followup", [("run_index", report)], staging_root=manifest.parent)


def test_smoke_attempt_chain_rejects_rewritten_or_singleton_ledger() -> None:
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
    case = {
        "pipeline": "late_fusion",
        "fallback": False,
        "errors": [],
        "citation_count": 1,
        "reference_count": 1,
        "youtube_start_url_count": 0,
        "references": [{"reference_id": "S1", "url": "https://example.test/source"}],
        "citation_ids": ["S1"],
    }
    cases = [dict(case, **spec) for spec in official_cases_payload()]
    payload = {"attempt_id": "attempt-001", "attempts": [attempt], "cases": cases, "cases_sha256": sha256_bytes(canonical_json(cases)), "official_cases_sha256": OFFICIAL_CASES_SHA256}
    attempt["cases_sha256"] = payload["cases_sha256"]
    attempt["attempt_sha256"] = sha256_bytes(canonical_json({key: value for key, value in attempt.items() if key != "attempt_sha256"}))
    payload["attempts"] = [attempt]
    assert _smoke_is_strict(payload)
    tampered = json.loads(json.dumps(payload))
    tampered["attempts"][0]["passed"] = False
    assert not _smoke_is_strict(tampered)
    singleton_without_chain = {"attempt_id": "attempt-001", "attempts": [{"attempt_id": "attempt-001"}], "cases": payload["cases"]}
    assert not _smoke_is_strict(singleton_without_chain)


def test_release_smoke_exception_persists_derived_attempt_ledger(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "accepted_manifest_v2", "run_id": "smoke-run", "sources": []}),
        encoding="utf-8",
    )
    output = tmp_path / "attempt-001" / "smoke.json"
    with (
        patch.object(late_fusion_activation_smoke, "create_rag", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(
            late_fusion_activation_smoke,
            "load_manifest_file",
            return_value=_stub_manifest_validation(manifest),
        ),
    ):
        with pytest.raises(RuntimeError, match="late_fusion_smoke_failed") as raised:
            asyncio.run(
                late_fusion_activation_smoke.run_smoke(
                    output,
                    manifest_path=manifest,
                    release_run_id="smoke-run",
                )
            )
    assert "boom" not in str(raised.value)
    ledger = output.parent / "smoke_attempts.json"
    assert ledger.is_file()
    attempts = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(attempts) == 1
    assert attempts[0]["passed"] is False
    assert json.loads(output.read_text(encoding="utf-8"))["attempts"] == attempts


def test_test_evidence_requires_bundled_summary_and_logs(tmp_path: Path) -> None:
    summary = {"passed": 4, "failed": 0, "skipped": 0, "warnings": 0, "subtests": 0}
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    checks = []
    for name, argv in {
        "pytest": ["python", "-m", "pytest"],
        "ruff": ["python", "-m", "ruff", "check"],
        "compileall": ["python", "-m", "compileall"],
        "diff_check": ["git", "diff", "--check"],
    }.items():
        path = tmp_path / f"{name}.log"
        path.write_text(name, encoding="utf-8")
        from scripts.build_release_test_evidence import _sha256

        checks.append({
            "name": name,
            "status": "passed",
            "exit_code": 0,
            "command_argv": argv,
            "cwd": str(tmp_path),
            "started_at": "2026-08-18T00:00:00+00:00",
            "finished_at": "2026-08-18T00:00:01+00:00",
            "duration_ms": 1000.0,
            "log_path": str(path),
            "log_bytes": path.stat().st_size,
            "log_sha256": _sha256(path),
        })
    payload = {"status": "passed", "summary": summary, "summary_path": str(summary_path), "summary_sha256": _sha256(summary_path), "checks": checks}
    assert _test_evidence(payload)["status"] == "passed"
    assert _test_evidence(payload, {})["status"] == "passed"
    summary_path.write_text("{}", encoding="utf-8")
    assert _test_evidence(payload)["status"] == "failed"


def test_verifier_exception_publishes_complete_failed_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "attempt-001"
    monkeypatch.setattr(build_release_verification, "build_report", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        build_release_verification.sys,
        "argv",
        ["build_release_verification.py", "--input-dir", str(tmp_path / "input"), "--run-dir", str(output), "--release-run-id", "run"],
    )
    assert build_release_verification.main() == 1
    assert output.exists()
    report = json.loads((output / "final_release_verification.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert (output / "verification_manifest.json").is_file()
